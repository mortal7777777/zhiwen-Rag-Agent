"""上下文工程与记忆服务。

包含四块能力：
1. token 估算与历史裁剪（分层上下文的预算控制）；
2. 会话滚动摘要：超过窗口的早期对话自动总结，保留关键信息；
3. 长期事实记忆：从对话提取用户偏好/背景，按语义召回注入；
4. 任务规划：复杂问题先生成执行计划（Plan-and-Execute 轻量版）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import repository as repo
from ..runtime_config import effective
from ..llm_text import message_text
from ..tracing import get_aux_usage_collector

logger = logging.getLogger(__name__)

# 记忆分类（与前端标签一一对应）
MEMORY_CATEGORIES = {
    "profile": "用户画像",
    "preference": "喜好偏好",
    "project": "工作项目",
    "decision": "重要决定",
    "lesson": "教训与应对",
    "other": "其他",
}

_consolidate_lock = threading.Lock()

# 记忆向量缓存：id -> (updated_at, vector)。全库精排要求每轮拿到全部记忆向量，
# 逐轮重算太浪费；按 updated_at 判新旧，只有新增/改动的记忆才重新嵌入。
_memory_vec_cache: dict[int, tuple[str, list[float]]] = {}
_memory_vec_lock = threading.Lock()


# ---------------- 结构化输出 schema（with_structured_output 用） ----------------


class FactItem(BaseModel):
    """一条值得长期记住的事实。"""

    category: str = Field(
        description="记忆分类，只能是：profile/preference/project/decision/lesson/other"
    )
    content: str = Field(description="一句话、第三人称的客观事实")


class FactsList(BaseModel):
    """事实提取输出：没有可提取内容时为空数组。"""

    facts: list[FactItem] = Field(default_factory=list)


class MergedFact(BaseModel):
    """整合后的一条记忆（合并多条原始记忆）。"""

    source_ids: list[int] = Field(description="被合并的原始记忆 id 列表")
    category: str = Field(description="记忆分类")
    content: str = Field(description="整合后的记忆内容")


class ConsolidateOutput(BaseModel):
    """记忆整合整理输出。"""

    facts: list[MergedFact] = Field(default_factory=list)
    archived_ids: list[int] = Field(
        default_factory=list, description="过时/被覆盖而归档的旧记忆 id"
    )
    summary: str = Field(default="", description="不超过 150 字的用户画像摘要")

# 显式"记住"信号：命中即强制提取长期记忆（不再只靠后台概率提取）
MEMORY_INTENT_PATTERNS = (
    "记住", "请记住", "请牢记", "牢记", "记得", "以后", "别忘了",
    "我喜欢", "我讨厌", "我爱", "我住在", "我是", "我的", "我目前在",
    "我在做", "目标是", "偏好", "希望", "将来",
)


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文约 1.5 字符/token，英文/数字约 4 字符/token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return max(1, int(cjk / 1.5 + other / 4))


# 注入去重阈值：同一主题常被切成多条近似重复的记忆，若不过滤，top_k 名额会被
# 一个话题的碎片吃光（整合前实测 5 条同义条目并存）。余弦超过该值只保留最相关一条。
_MEMORY_DEDUP_SIM = 0.9


def _cosine(a, b) -> float:
    """余弦相似度（显式归一，不假设向量已单位化）。"""
    try:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        if na and nb:
            return dot / (na * nb)
        return dot
    except Exception:
        return 0.0


def _iso(value) -> str | None:
    """datetime → ISO 字符串（审计快照用；None 原样返回）。"""
    try:
        return value.isoformat() if value is not None else None
    except Exception:
        return str(value)


def _date_of(memory: dict) -> str:
    """记忆的录入日期（YYYY-MM-DD）：提示词与注入前缀的时效锚点。

    优先 created_at（事实首次入库时间）；老数据缺失时退回 updated_at。
    """
    text = _iso(memory.get("created_at") or memory.get("updated_at")) or ""
    return text[:10] if len(text) >= 10 else "未知"


def _write_memory_audit(settings, record: dict) -> str:
    """把一次记忆整合的"变更前快照 + 结果"追加到 meta_dir/memory_audit/YYYY-MM-DD.jsonl。

    整合是唯一批量改记忆状态的路径，此前只留一个时间戳——出了问题既无法回溯，
    也无法区分"模型归档的"还是"用户手工归档的"。写失败不影响整合本身。
    返回文件路径（失败时返回空串）。
    """
    try:
        base = getattr(settings, "meta_dir", None)
        if base is None:
            return ""
        audit_dir = base / "memory_audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        path = audit_dir / f"{datetime.now():%Y-%m-%d}.jsonl"
        payload = {"ts": datetime.now().isoformat(), **record}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return str(path)
    except Exception as exc:
        logger.warning("记忆整合审计写入失败：%s", exc)
        return ""


def context_window_for(settings) -> int:
    """解析当前对话模型的上下文窗口（token）。

    优先显式配置（MODEL_CONTEXT_WINDOW）；否则按模型名推断：`[1m]` 长窗口
    后缀 → 1M（仅 Anthropic 格式端点支持，见 README），其余按 128K 保守估计。
    """
    explicit = int(getattr(settings, "model_context_window", 0) or 0)
    if explicit > 0:
        return explicit
    name = ""
    try:
        active = str(effective(settings, "active_chat_provider") or "")
        for provider in effective(settings, "providers") or []:
            if str(provider.get("id")) == active:
                name = str(provider.get("model") or "")
                break
    except Exception:
        name = ""
    name = name.lower()
    if "[1m]" in name:
        return 1_000_000
    return 128_000


def trim_history_for_budget(rows: list[dict], budget_tokens: int) -> list[dict]:
    """从最旧的消息开始裁剪，直到总 token 不超过预算（保留最近的消息）。

    加最小回收门控（借鉴 Hermes min_reclaim 思路）：只有裁剪确实能
    回收显著 token 时才动手。裁剪会改写已发送的历史、破坏 provider
    前缀缓存——每轮小幅裁剪会让缓存前缀几乎每轮都断，命中率崩塌；
    一次性回收足够多才值得断一次缓存。回收 < 15% 预算时放弃裁剪，
    让上层压缩（compact_conversation）处理。
    """
    if budget_tokens <= 0:
        return rows[-1:]
    kept: list[dict] = []
    total = 0
    for row in reversed(rows):
        tokens = estimate_tokens(row["content"])
        if total + tokens > budget_tokens:
            break
        kept.append(row)
        total += tokens
    kept.reverse()
    # 最小回收门控：裁剪掉的 token 不足预算 15% 时不值得断缓存前缀
    total_all = sum(estimate_tokens(r["content"]) for r in rows)
    reclaimed = total_all - total
    if reclaimed < budget_tokens * 0.15:
        return rows
    return kept


class ContextService:
    """把记忆 / 摘要 / 规划组装成分层上下文。"""

    def __init__(self, settings: Settings, chat: ChatOpenAI):
        self.settings = settings
        # 用低温模型做摘要/提取/规划，保证结果稳定可复现
        self.chat = chat

    def _invoke(self, messages):
        """调用低温模型时按当前线程注入辅助用量收集器（与主循环分开统计）。"""
        return self.chat.invoke(
            messages,
            config={"callbacks": [get_aux_usage_collector()]},
        )

    def _structured(self, schema: type[BaseModel], prompt: str):
        """结构化输出（with_structured_output，工具调用式约束 schema）。

        比旧的"正文 JSON + 正则贪婪匹配"稳：模型不合规时返回 None
        或抛异常，由调用方回退旧解析路径，不影响主流程。
        """
        llm = self.chat.with_structured_output(schema)
        return llm.invoke(
            [HumanMessage(content=prompt)],
            config={"callbacks": [get_aux_usage_collector()]},
        )

    # ---------------- 会话滚动摘要 ----------------

    def compact_conversation(
        self,
        db: Session,
        conversation_id: int,
    ) -> tuple[str | None, list[dict]]:
        """软窗口压缩：返回 (摘要, 发送窗口)。

        策略：
        - 窗口起点粘滞：窗口 = 摘要边界之后的消息（summary_up_to_id），
          没触发压缩时逐字节不变——provider 前缀缓存跨轮稳定命中；
        - 高水位触发：窗口条数超过上限 1.5 倍（或 token 超预算 1.25 倍）
          才压缩，一次压到上限条数；
        - 预算按模板任务差异化（knowledge 检索为主用 24k，coding/writing 36k）。

        为什么是高水位而不是"超上限就压"：压缩会把窗口起点前移、改写历史
        开头，前缀缓存从第一行起失效。若压到恰好等于上限，工具轮较多的
        会话下一轮立刻又超限、又只丢几条，导致每轮压缩一次、缓存每轮断
        （实测每轮首调只命中静态头 ~8.5k token，命中率从 ~90% 掉到 ~29%）。
        留出 0.5× 上限的余量后，压缩一次可稳定多轮命中，与
        trim_history_for_budget 的 15% 回收门控同一思路。
        """
        rows = repo.list_messages_with_id(db, conversation_id, limit=500)
        if not rows:
            return None, []
        max_messages = int(effective(self.settings, "history_max_messages") or 400)
        budget = self.history_budget_for(db, conversation_id)
        old_summary, up_to = repo.get_summary_state(db, conversation_id)
        window = [r for r in rows if r["id"] > up_to]
        if not window:
            # 摘要边界晚于全部可见消息（消息被清理/编辑过）：退回全量
            window = rows
        window_tokens = sum(estimate_tokens(r["content"]) for r in window)
        # 高水位：条数 1.5×上限 或 token 1.25×预算 才触发（压缩一次值得断一次缓存）
        high_messages = max_messages + max_messages // 2
        high_tokens = int(budget * 1.25)
        if len(window) <= high_messages and window_tokens <= high_tokens:
            return old_summary, window

        cutoff = max(0, len(window) - max(4, max_messages))
        if cutoff == 0:
            # 超预算但条数很少（单条超长）：不压缩，交给 trim 裁剪
            return old_summary, window
        cutoff_id = window[cutoff - 1]["id"]
        batch = window[:cutoff]
        summary = self._summarize(batch, old_summary)
        if not summary:
            # 摘要失败不推进边界：否则窗口前移但内容没进摘要，静默丢历史
            return old_summary, window
        repo.save_summary(db, conversation_id, summary, cutoff_id)
        return summary, window[cutoff:]

    def context_stats(self, db: Session, conversation_id: int) -> dict:
        """上下文占用统计（/context 命令与 Web 上下文面板共用）。"""
        rows = repo.list_messages_with_id(db, conversation_id, limit=500)
        budget = self.history_budget_for(db, conversation_id)
        max_messages = int(effective(self.settings, "history_max_messages") or 400)
        summary, up_to = repo.get_summary_state(db, conversation_id)
        covered = sum(1 for r in rows if r["id"] <= up_to)
        # 发送窗口口径与 compact_conversation 保持一致：起点=摘要边界（粘滞），
        # 1.5×条数 或 1.25×token 高水位才触发压缩
        window = [r for r in rows if r["id"] > up_to] or rows
        window_tokens = sum(estimate_tokens(r["content"]) for r in window)
        high_messages = max_messages + max_messages // 2
        trigger = len(window) > high_messages or window_tokens > int(budget * 1.25)
        effective_messages = len(window) - (
            max(0, len(window) - max(4, max_messages)) if trigger else 0
        )
        # 最近运行的缓存命中统计（主循环口径，供上下文面板展示）
        cache_hit = 0
        cache_miss = 0
        recent_llm_calls = 0
        recent_runs = 0
        try:
            runs = repo.list_agent_runs(db, limit=10, conversation_id=conversation_id)
            for r in runs:
                usage = r.get("token_usage")
                if not isinstance(usage, dict):
                    continue
                hit = usage.get("cache_hit_tokens") or 0
                miss = usage.get("cache_miss_tokens") or 0
                if hit or miss:
                    cache_hit += int(hit)
                    cache_miss += int(miss)
                recent_llm_calls += int(usage.get("llm_calls") or 0)
                recent_runs += 1
        except Exception as exc:
            logger.warning("上下文面板读取运行统计失败：%s", exc)
        return {
            "conversation_id": conversation_id,
            "message_count": len(rows),
            # 下一轮实际发送的历史条数（窗口；压缩后比 message_count 少）
            "effective_history_messages": effective_messages,
            "history_max_messages": max_messages,
            # 压缩触发水位（1.5×上限）：窗口超过它才压缩到上限条数
            "history_high_water_messages": high_messages,
            "estimated_tokens": window_tokens,
            "token_budget": budget,
            "summary_chars": len(summary or ""),
            "summary_up_to_id": up_to,
            "summary_covered_messages": covered,
            "compaction_would_trigger": trigger,
            # 最近 N 次运行：主循环 LLM 调用数与缓存命中率（0 数据时为 None）
            "recent_runs": recent_runs,
            "recent_llm_calls": recent_llm_calls,
            "recent_cache_hit_tokens": cache_hit,
            "recent_cache_miss_tokens": cache_miss,
            "recent_cache_hit_rate": (
                cache_hit / (cache_hit + cache_miss)
                if (cache_hit + cache_miss) > 0
                else None
            ),
        }

    def compact_now(self, db: Session, conversation_id: int) -> dict:
        """手动强制压缩（/compact）：保留最近一段原始消息，其余并入滚动摘要。

        与 compact_conversation 的区别：不看软窗口条件，立即压缩；
        保留条数取 max(4, 总数/4)，上限 history_max_messages——
        类 Claude Code /compact 的"保留近期轮次、总结更早内容"语义。
        """
        rows = repo.list_messages_with_id(db, conversation_id, limit=500)
        if len(rows) <= 4:
            return {
                "compacted": False,
                "reason": "消息太少，无需压缩",
                "kept_messages": len(rows),
                "summarized_messages": 0,
                "summary_chars": 0,
            }
        keep = max(
            4,
            min(
                int(effective(self.settings, "history_max_messages") or 400),
                len(rows) // 4,
            ),
        )
        cutoff = len(rows) - keep
        cutoff_id = rows[cutoff - 1]["id"]
        old_summary, up_to = repo.get_summary_state(db, conversation_id)
        batch = [r for r in rows if up_to < r["id"] <= cutoff_id]
        summary = self._summarize(batch, old_summary) if batch else old_summary
        if not summary:
            return {
                "compacted": False,
                "reason": "摘要生成失败，已保留原始消息",
                "kept_messages": len(rows),
                "summarized_messages": 0,
                "summary_chars": 0,
            }
        repo.save_summary(db, conversation_id, summary, cutoff_id)
        return {
            "compacted": True,
            "kept_messages": keep,
            "summarized_messages": len(batch),
            "summary_chars": len(summary),
        }

    def history_budget_for(self, db: Session, conversation_id: int) -> int:
        """按会话绑定的模板类别返回历史上下文 token 预算（config.history_budget_*）。

        压缩（compact_conversation）与压缩后裁剪（trim_history_for_budget）
        必须用同一个预算，否则按模板调大的预算会被兜底值悄悄砍回去。
        最终预算会被模型上下文窗口夹紧（窗口 − 固定开销），换小窗口模型
        时不会把预算撑爆导致超窗报错。
        """
        default = int(effective(self.settings, "history_max_tokens") or 64000)
        by_category = {
            "general": self.settings.history_budget_general,
            "knowledge": self.settings.history_budget_knowledge,
            "coding": self.settings.history_budget_coding,
            "writing": self.settings.history_budget_writing,
            "translate": self.settings.history_budget_translate,
        }
        budget = default
        try:
            conv = repo.get_conversation(db, conversation_id)
            if conv is not None and getattr(conv, "template_id", None):
                tpl = repo.get_template(db, conv.template_id)
                if tpl is not None:
                    budget = by_category.get(tpl.category, default)
        except Exception:
            pass
        return self._clamp_to_context_window(budget)

    def _clamp_to_context_window(self, budget: int) -> int:
        """把历史预算夹到模型窗口之内（窗口 − 静态区/工具/D 块/输出的预留）。"""
        window = context_window_for(self.settings)
        if window <= 0:
            return budget
        reserve = int(getattr(self.settings, "context_window_reserve", 0) or 0)
        ceiling = max(8000, window - reserve)
        if budget > ceiling:
            logger.info(
                "历史预算 %d 超过模型窗口可用量 %d（窗口 %d − 预留 %d），已夹紧",
                budget, ceiling, window, reserve,
            )
            return ceiling
        return budget

    def _summarize(self, batch: list[dict], existing: str | None) -> str | None:
        """把一批消息并入滚动摘要；失败返回 None（调用方不得推进摘要边界）。"""
        # 输入预算：旧摘要先裁剪再拼给模型，避免多次压缩后摘要膨胀
        existing = (existing or "")[: self.settings.summary_max_chars]
        text = "\n".join(f"{r['role']}: {r['content']}" for r in batch)
        prompt = f"""把以下对话内容压缩成一份简洁摘要，要求：
1. 保留关键信息：讨论主题、主要观点、用户偏好、背景信息、做出的决定；
2. 使用中文，第三人称；
3. 如果提供了已有摘要，要在其基础上合并新增内容，而不是重复；
4. 只输出摘要本身，总长度不超过 {self.settings.summary_max_chars} 字。

已有摘要：
{existing or "（无）"}

新增对话内容：
{text[:20000]}"""
        try:
            resp = self._invoke([HumanMessage(content=prompt)])
            summary = message_text(resp.content).strip()
            if summary:
                return summary[: self.settings.summary_max_chars * 2]
        except Exception as exc:
            logger.warning("会话摘要生成失败：%s", exc)
        return None

    # ---------------- 长期事实记忆 ----------------

    def retrieve_memories(
        self,
        db: Session,
        embeddings,
        question: str,
    ) -> list[str]:
        """按语义召回相关记忆：**全库向量精排**（向量带缓存，缺失才补算）。

        此前用 n-gram 粗筛候选再向量化——记忆写"用 PyCharm"、问题问"IDE 用什么"
        这类换说法就召不回。记忆库量级（几十~几百条）下直接全库精排成本可控：
        向量按 id+updated_at 缓存后，每轮只多一次 query 向量化。
        """
        if not self.settings.memory_enabled:
            return []
        scan_limit = int(getattr(self.settings, "memory_recall_scan_limit", 500) or 500)
        # order="created"：兜底要的是"最近录入的事实"，而不是"最近被编辑过的"
        # （updated_at 只反映内容编辑；且并列时若无 tiebreaker，取前 N 条是任意的）
        memories = repo.list_memories(db, limit=scan_limit, order="created")
        if not memories:
            return []
        try:
            q_vec = embeddings.embed_query(question)
            vectors = self._memory_vectors(embeddings, memories)
            scored: list[dict] = []
            for memory, vec in zip(memories, vectors):
                if vec is None:
                    continue
                sim = sum(a * b for a, b in zip(q_vec, vec))
                if sim >= self.settings.memory_min_score:
                    scored.append(
                        {
                            "sim": sim,
                            "id": int(memory["id"]),
                            "content": str(memory.get("content") or ""),
                            "category": str(memory.get("category") or ""),
                            "date": _date_of(memory),
                            "vec": vec,
                        }
                    )
            scored.sort(key=lambda x: x["sim"], reverse=True)
            # 贪心选取 + 近似去重：与已选条目近乎同向的候选跳过。同一主题常被切成
            # 多条近似重复的记忆，不去重的话 3 个名额会被一个话题的碎片吃光，
            # 模型拿到的只是重叠信息（整合前实测 5 条同义条目并存）。
            picked: list[dict] = []
            for cand in scored:
                if len(picked) >= self.settings.memory_top_k:
                    break
                if any(
                    _cosine(cand["vec"], p["vec"]) > _MEMORY_DEDUP_SIM for p in picked
                ):
                    continue
                picked.append(cand)
            # 注入时带 [分类|录入日期] 锚点：原来的裸句子既不标时效也不标类别，
            # 模型无法判断"这条偏好是三个月前的还是今天的"，只能一律存疑。
            selected = [
                (f"[{p['category']}|{p['date']}] " if (p["category"] or p["date"]) else "")
                + p["content"]
                for p in picked
            ]
            top_ids = [p["id"] for p in picked]
            if not selected:
                # 零命中才用最近记忆兜底（"介绍一下我"这类问候与具体事实
                # 相似度低，需要一段背景撑底）；兜底不算"命中"。
                # 注入格式与主路径一致（带 [分类|录入日期] 锚点）——prepare 的
                # 提示语已声明每条格式为 [分类|录入日期]，裸句子会让模型误判格式。
                selected = [
                    (
                        f"[{m.get('category')}|{_date_of(m)}] "
                        if (m.get("category") or _date_of(m))
                        else ""
                    )
                    + str(m.get("content") or "")
                    for m in memories[: self.settings.memory_empty_fallback]
                ]
                top_ids = []
            # token 预算裁剪（从尾部去掉超预算的记忆）；被裁掉的不计命中
            total = 0
            kept: list[str] = []
            kept_ids: list[int] = []
            for idx, content in enumerate(selected):
                tokens = estimate_tokens(content)
                if total + tokens > self.settings.memory_max_tokens:
                    break
                kept.append(content)
                if idx < len(top_ids):
                    kept_ids.append(top_ids[idx])
                total += tokens
            if kept_ids and db is not None:
                try:
                    repo.bump_memory_hits(db, kept_ids)
                except Exception as exc:
                    logger.debug("记忆命中计数失败：%s", exc)
            return kept
        except Exception as exc:
            logger.warning("记忆召回失败：%s", exc)
            return []

    def _memory_vectors(self, embeddings, memories: list[dict]) -> list[list[float] | None]:
        """批量取记忆向量：命中缓存直接用（按 id + updated_at 判新旧），缺失才补算。"""
        vectors: list[list[float] | None] = [None] * len(memories)
        missing: list[int] = []
        with _memory_vec_lock:
            for i, memory in enumerate(memories):
                key = str(memory.get("updated_at") or "")
                cached = _memory_vec_cache.get(int(memory["id"]))
                if cached is not None and cached[0] == key:
                    vectors[i] = cached[1]
                else:
                    missing.append(i)
        if not missing:
            return vectors
        texts = [str(memories[i].get("content") or "") for i in missing]
        try:
            fresh = embeddings.embed_documents(texts, batch_size=64)
        except Exception as exc:
            logger.warning("记忆向量化失败：%s", exc)
            return vectors
        with _memory_vec_lock:
            for i, vec in zip(missing, fresh):
                vec_list = list(vec)
                vectors[i] = vec_list
                _memory_vec_cache[int(memories[i]["id"])] = (
                    str(memories[i].get("updated_at") or ""),
                    vec_list,
                )
        return vectors

    def get_memory_summary(self, db: Session) -> str | None:
        """常驻的用户画像摘要（由整合整理生成，始终注入的小段记忆）。"""
        if not self.settings.memory_enabled:
            return None
        return repo.get_meta(db, "memory_summary")

    def has_memory_intent(self, user_text: str) -> bool:
        """检测用户是否明确要求"记住"（显式记忆信号）。"""
        if not user_text:
            return False
        return any(p in user_text for p in MEMORY_INTENT_PATTERNS)

    def extract_facts(
        self,
        user_text: str,
        assistant_text: str,
        force: bool = False,
    ) -> list[str]:
        """从一轮对话中提取值得长期记住的事实，返回 [{category, content}]。

        force=True：用户明确要求"记住…"时，prompt 强调完整提取、
        把用户原话中的具体信息（偏好/背景/事实）逐条保留。
        """
        if not self.settings.memory_enabled or not user_text:
            return []
        categories = "、".join(
            f"{k}（{v}）" for k, v in MEMORY_CATEGORIES.items()
        )
        force_note = (
            "用户明确要求'记住/以后记得'等信息，这是显式记忆指令："
            "请把用户提到的个人信息、偏好、背景、事实尽可能完整地逐条提取，"
            "不要遗漏，也不要加入推测内容。\n"
            if force
            else ""
        )
        prompt = f"""从以下一轮对话中提取值得长期记住的事实。
记忆分类（category 只允许以下值）：{categories}
要求：
1. 【不记可推导的】凡是读项目文件/代码/配置/文档就能直接得到的信息，一律不提取：
   目录结构与文件清单、代码行数、模块职责、实现细节、依赖与版本、已有文档内容——
   记忆只存"从上下文推不出来的东西"；
2. 【该记什么】只提取：用户的长期偏好与沟通习惯、做过的选择及其理由、
   外部约束（设备/环境/团队/时间）、只有对话里才能得知的背景与历史包袱；
   **只以「用户」消息里明确说出的内容为依据**——助手的回答（哪怕在复述
   用户信息或已有记忆）不算依据，不要据此生成"新"记忆（会造成自我复读式重复）；
3. 每条用一句话、第三人称客观描述，不要推测；一次性问题、闲聊、临时指令不记；
4. 如果用户在纠正或覆盖之前的信息（例如"我不住北京了"），
   提取为描述"当前状态"的新事实，方便后续整理时覆盖旧记忆；
{force_note}
5. 输出 JSON 数组，元素形如 {{"category": "profile", "content": "..."}}，
   没有可提取内容时输出 []，不要任何解释。

用户：{user_text[:1500]}
助手：{assistant_text[:1500]}"""
        # 优先结构化输出（schema 约束，弱模型下比正则解析稳）
        try:
            out = self._structured(FactsList, prompt)
            if out is not None and getattr(out, "facts", None) is not None:
                facts = []
                for item in out.facts:
                    text = str(item.content or "").strip()
                    category = str(item.category or "other").strip()
                    if not text:
                        continue
                    if category not in MEMORY_CATEGORIES:
                        category = "other"
                    facts.append({"category": category, "content": text})
                return facts
        except Exception as exc:
            logger.warning("结构化事实提取失败，回退正则解析：%s", exc)
        # 回退：正文 JSON + 正则抽取（旧路径）
        try:
            resp = self._invoke([HumanMessage(content=prompt)])
            content = message_text(resp.content).strip()
            match = re.search(r"\[.*\]", content, re.DOTALL)
            if not match:
                return []
            data = json.loads(match.group(0))
            facts = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("content", "")).strip()
                category = str(item.get("category", "other")).strip()
                if text and category in MEMORY_CATEGORIES:
                    facts.append({"category": category, "content": text})
                elif text:
                    facts.append({"category": "other", "content": text})
            return facts
        except Exception as exc:
            logger.warning("事实记忆提取失败：%s", exc)
            return []

    def add_facts(
        self,
        db: Session,
        embeddings,
        facts: list[dict],
        source_conversation_id: int | None,
    ) -> int:
        """按去重后写入记忆库，返回新增条数。

        去重对**全库**比较（文本精确 + 向量相似）：只比对最近若干条时，
        库中更早的近似重复会漏判，记忆库会缓慢膨胀（每条近似重复都会
        被检索/注入/整合一遍）。既有向量一次性算好，避免逐条重复向量化。
        """
        existing = repo.list_memories(db, limit=1000)
        existing_texts = [m["content"] for m in existing]
        existing_vecs: list = []
        try:
            if existing_texts:
                existing_vecs = list(
                    embeddings.embed_documents(existing_texts, batch_size=64)
                )
        except Exception as exc:
            logger.warning("既有记忆向量化失败，降级为纯文本去重：%s", exc)
        added = 0
        for fact in facts:
            content = fact["content"]
            category = fact.get("category", "other")
            if self._is_duplicate(content, existing_texts, existing_vecs, embeddings):
                continue
            repo.add_memory(db, content, category=category, source_conversation_id=source_conversation_id)
            existing_texts.append(content)
            try:
                existing_vecs.append(embeddings.embed_query(content))
            except Exception:
                pass
            added += 1
        return added

    def maybe_consolidate(self, db: Session) -> dict | None:
        """按间隔自动整合记忆（供 Agent 每轮后台调用）。"""
        if not self.settings.memory_enabled:
            return None
        last_raw = repo.get_meta(db, "memory_last_consolidated_at")
        last = None
        if last_raw:
            try:
                last = datetime.fromisoformat(last_raw)
            except Exception:
                last = None
        if last is not None and (
            datetime.now() - last
            < timedelta(
                hours=int(effective(self.settings, "memory_consolidate_interval_hours") or 24)
            )
        ):
            return None
        active = [m for m in repo.list_memories(db, limit=2000)]
        if len(active) < int(effective(self.settings, "memory_consolidate_threshold") or 10):
            return None
        return self.consolidate_memories(db)

    def consolidate_memories(self, db: Session) -> dict:
        """整合整理：合并重复、新事实覆盖旧事实，并生成画像摘要。

        2026-09-30 收紧归档语义：这里的目标是**去重**，不是压缩总量。此前提示词含
        "删除琐碎无用的"+ 按命中次数优先归档，实测把 46 条压成 2 条（44 条归档，
        其中 33 条曾被召回），召回层只剩两个混着多主题的大块，反而更差。现在只允许
        归档"被合并的重复项"与"被新事实明确覆盖的旧项"。
        """
        with _consolidate_lock:
            memories = repo.list_all_memories(db, limit=2000)
            active = [m for m in memories if m["status"] == "active"]
            if not active:
                return {"merged": 0, "archived": 0, "summary": None}

            # 按 created_at 升序交给模型：与提示词"旧记忆在前、新记忆在后"的前提一致。
            # 此前用 list_all_memories 的 updated_at DESC（最新在前），前提是反的；
            # 且 updated_at 当时会被召回刷新，连"最新"本身也不可信。
            active_sorted = sorted(
                active,
                key=lambda m: (str(m.get("created_at") or ""), int(m.get("id") or 0)),
            )
            before_snapshot = [
                {
                    "id": m["id"],
                    "category": m.get("category"),
                    "status": m.get("status"),
                    "hit_count": m.get("hit_count") or 0,
                    "created_at": _iso(m.get("created_at")),
                    "content": m.get("content"),
                }
                for m in active_sorted
            ]
            lines = "\n".join(
                f"{m['id']}: [{m['category']}]"
                f"（录入 {_date_of(m)}，命中 {m.get('hit_count') or 0} 次）{m['content']}"
                for m in active_sorted
            )
            prompt = f"""以下是系统长期记忆库中的全部活跃记忆，按录入时间**从旧到新**排列
（格式：id: [分类]（录入日期，命中次数）内容）。

请做一次记忆整合整理。**目标是去掉重复，不是压缩总量**：
1. 合并内容重复或高度相似的多条记忆为一条（这是 archived_ids 的主要正当来源）；
2. 仅当同一事实存在明确矛盾的多个版本时才覆盖：以**录入日期较新**的为准，
   把被覆盖的旧版本 id 放入 archived_ids；
3. **不要**因为"看起来琐碎""价值不高""可推导"就归档——活跃记忆过少会让召回退化成
   少数几个混着多个主题的大块，语义匹配反而更不准（这是已经踩过的坑）；
4. 命中次数仅供参考，**不作为归档依据**（旧记忆可能只是还没来得及被召回）；
5. 不属于第 1、2 类的记忆一律原样保留：不要出现在 facts 里，也不要放进 archived_ids；
6. 生成一份不超过 150 字的"用户画像摘要"（第三人称概括用户身份、喜好、项目）。

输出 JSON（不要任何解释）：
{{
  "facts": [{{"source_ids": [多个原id], "category": "profile", "content": "整合后内容"}}],
  "archived_ids": [被覆盖的旧id],
  "summary": "用户画像摘要"
}}

记忆列表：
{lines[:12000]}"""
            # 优先结构化输出（schema 约束 source_ids/archived_ids 为整数数组）
            data = None
            try:
                out = self._structured(ConsolidateOutput, prompt)
                if out is not None:
                    data = out.model_dump()
            except Exception as exc:
                logger.warning("结构化记忆整合失败，回退正则解析：%s", exc)
            if data is None:
                # 回退：正文 JSON + 正则抽取（旧路径）
                try:
                    resp = self._invoke([HumanMessage(content=prompt)])
                    content = message_text(resp.content).strip()
                    match = re.search(r"\{.*\}", content, re.DOTALL)
                    if not match:
                        return {"merged": 0, "archived": 0, "summary": None}
                    data = json.loads(match.group(0))
                except Exception as exc:
                    logger.warning("记忆整合失败：%s", exc)
                    return {"merged": 0, "archived": 0, "summary": None}

            active_by_id = {int(m["id"]): m for m in active_sorted}
            archived_ids = set(int(x) for x in (data.get("archived_ids") or []))
            # 只记"真正被翻转的那几行"：模型可能给出已归档/不存在的 id，
            # 若把请求集合直接当成结果，返回的 archived 数会虚报，
            # 审计记录与下面的"过度删除"告警也会跟着失真。
            actually_archived: set[int] = set()
            for m in active:
                if m["id"] in archived_ids:
                    repo.update_memory(db, m["id"], status="archived")
                    actually_archived.add(int(m["id"]))

            merged_count = 0
            merged_sources: set[int] = set()
            for fact in data.get("facts") or []:
                source_ids = [int(x) for x in (fact.get("source_ids") or [])]
                if not source_ids:
                    continue
                category = (
                    fact.get("category", "other")
                    if fact.get("category") in MEMORY_CATEGORIES
                    else "other"
                )
                new_content = str(fact.get("content", "")).strip()
                if not new_content:
                    continue
                # 主记录取"录入时间最新"的那条（此前用 max(id)：id 只是自增序，
                # 合并后幸存行会保留最老的 created_at、拿到最新的 content）
                primary = max(
                    source_ids,
                    key=lambda sid: (
                        str((active_by_id.get(sid) or {}).get("created_at") or ""),
                        sid,
                    ),
                )
                repo.update_memory(db, primary, content=new_content, category=category)
                for sid in source_ids:
                    if sid != primary:
                        repo.update_memory(db, sid, status="archived")
                        actually_archived.add(sid)
                        merged_sources.add(sid)
                merged_count += 1

            summary = str(data.get("summary", "")).strip()
            if summary:
                repo.set_meta(db, "memory_summary", summary)
            repo.set_meta(db, "memory_last_consolidated_at", datetime.now().isoformat())

            # 审计：整合是唯一会批量改状态的路径，此前只有一个时间戳、无从回溯。
            # 快照落 JSONL（meta_dir/memory_audit/YYYY-MM-DD.jsonl），可回滚可复盘。
            try:
                active_after = len(repo.list_memories(db, limit=2000))
            except Exception:
                active_after = len(active_sorted) - len(actually_archived)
            audit_path = _write_memory_audit(
                self.settings,
                {
                    "merged": merged_count,
                    "archived_ids": sorted(actually_archived),
                    "merged_source_ids": sorted(merged_sources),
                    "active_before": len(active_sorted),
                    "active_after": active_after,
                    "summary": summary or None,
                    "before": before_snapshot,
                },
            )
            # 只归档"非合并项"= 删除而非去重：给一个可观测的告警
            # （历史事故：46 条压成 2 条，其中 33 条曾被召回）
            deleted_like = actually_archived - merged_sources
            if len(deleted_like) > max(2, len(active_sorted) // 2):
                logger.warning(
                    "记忆整合归档了 %d 条非合并项（活跃 %d → %d），疑似过度删除；"
                    "明细见 %s，可在记忆面板恢复",
                    len(deleted_like),
                    len(active_sorted),
                    active_after,
                    audit_path,
                )
            return {
                "merged": merged_count,
                "archived": len(actually_archived),
                "summary": summary or None,
            }

    @staticmethod
    def _is_duplicate(
        fact: str,
        existing_texts: list[str],
        existing_vecs: list,
        embeddings,
    ) -> bool:
        """与全库既有记忆比对（文本精确 + 向量相似度 >0.95）。"""
        for old in existing_texts:
            if fact == old:
                return True
        if not existing_texts:
            return False
        try:
            v1 = embeddings.embed_query(fact)
            for v2 in existing_vecs or []:
                sim = sum(a * b for a, b in zip(v1, v2))
                if sim > 0.95:
                    return True
        except Exception:
            pass
        return False
