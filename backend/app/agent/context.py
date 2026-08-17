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


# ---------------- 结构化输出 schema（with_structured_output 用） ----------------


class PlanSteps(BaseModel):
    """任务规划输出：简单问题返回空数组。"""

    steps: list[str] = Field(
        default_factory=list, description="2~5 步执行计划，一步即可完成时为空数组"
    )


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

# 不同任务模板的历史上下文预算（token）：检索型任务历史权重低、写作/编程需要更多上下文
TEMPLATE_HISTORY_BUDGET = {
    "general": 32000,
    "knowledge": 24000,
    "coding": 36000,
    "writing": 36000,
    "translate": 24000,
}

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
        """软窗口压缩：返回 (摘要, 最近消息)。

        策略（按用户建议调整）：
        - 条数与 token 双条件：只有"消息数超过上限 且/或 token 超预算"才压缩，
          短会话即使轮数多也不过早压缩；
        - 预算按模板任务差异化（knowledge 检索为主用 24k，coding/writing 36k）；
        - 无论如何保留最近 history_max_messages 条原始消息（底线）。
        """
        rows = repo.list_messages_with_id(db, conversation_id, limit=500)
        if not rows:
            return None, []
        max_messages = self.settings.history_max_messages
        budget = self._history_budget_for(db, conversation_id)
        total_tokens = sum(estimate_tokens(r["content"]) for r in rows)
        # 软窗口：条数未超上限且 token 未超预算 -> 全部保留，不做压缩
        if len(rows) <= max_messages and total_tokens <= budget:
            return None, rows

        cutoff = max(0, len(rows) - max_messages)
        if cutoff == 0:
            # 超预算但条数很少（单条超长）：不压缩，交给 trim 裁剪
            return None, rows
        cutoff_id = rows[cutoff - 1]["id"]
        old_summary, up_to = repo.get_summary_state(db, conversation_id)
        if up_to >= cutoff_id:
            return old_summary, rows[cutoff:]

        batch = [r for r in rows if r["id"] > up_to and r["id"] <= cutoff_id]
        if batch:
            summary = self._summarize(batch, old_summary)
            repo.save_summary(db, conversation_id, summary, cutoff_id)
            return summary, rows[cutoff:]
        return old_summary, rows[cutoff:]

    def context_stats(self, db: Session, conversation_id: int) -> dict:
        """上下文占用统计（/context 命令与 Web 上下文面板共用）。"""
        rows = repo.list_messages_with_id(db, conversation_id, limit=500)
        total_tokens = sum(estimate_tokens(r["content"]) for r in rows)
        budget = self._history_budget_for(db, conversation_id)
        max_messages = self.settings.history_max_messages
        summary, up_to = repo.get_summary_state(db, conversation_id)
        covered = sum(1 for r in rows if r["id"] <= up_to)
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
            "history_max_messages": max_messages,
            "estimated_tokens": total_tokens,
            "token_budget": budget,
            "summary_chars": len(summary or ""),
            "summary_up_to_id": up_to,
            "summary_covered_messages": covered,
            "compaction_would_trigger": len(rows) > max_messages or total_tokens > budget,
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
        keep = max(4, min(self.settings.history_max_messages, len(rows) // 4))
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

    def _history_budget_for(self, db: Session, conversation_id: int) -> int:
        """按会话绑定的模板类别返回历史上下文预算。"""
        default = self.settings.history_max_tokens
        try:
            conv = repo.get_conversation(db, conversation_id)
            if conv is not None and getattr(conv, "template_id", None):
                tpl = repo.get_template(db, conv.template_id)
                if tpl is not None:
                    return TEMPLATE_HISTORY_BUDGET.get(tpl.category, default)
        except Exception:
            pass
        return default

    def _summarize(self, batch: list[dict], existing: str | None) -> str:
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
            summary = (resp.content or "").strip()
            if summary:
                return summary[: self.settings.summary_max_chars * 2]
        except Exception as exc:
            logger.warning("会话摘要生成失败：%s", exc)
        return existing or ""

    # ---------------- 长期事实记忆 ----------------

    def retrieve_memories(
        self,
        db: Session,
        embeddings,
        question: str,
    ) -> list[str]:
        """按语义召回相关记忆：先粗筛候选，再 embed，避免每轮全量向量化。"""
        if not self.settings.memory_enabled:
            return []
        memories = repo.list_memories(db, limit=500)
        if not memories:
            return []
        candidates = self._memory_candidates(question, memories)
        if not candidates:
            return []
        try:
            q_vec = embeddings.embed_query(question)
            texts = [m["content"] for m in candidates]
            doc_vecs = embeddings.embed_documents(texts, batch_size=64)
            scored = []
            for memory, vec in zip(candidates, doc_vecs):
                sim = sum(a * b for a, b in zip(q_vec, vec))
                if sim >= self.settings.memory_min_score:
                    scored.append((sim, memory["content"]))
            scored.sort(key=lambda x: x[0], reverse=True)
            selected = [content for _, content in scored[: self.settings.memory_top_k]]
            # 相关性不足时用最近记忆补位：
            # "介绍一下我"这类问题与具体事实的向量相似度可能偏低，
            # 而近期记忆对个人助手通常同样重要（memories 已按更新时间倒序）。
            for memory in memories:
                if len(selected) >= self.settings.memory_top_k:
                    break
                if memory["content"] not in selected:
                    selected.append(memory["content"])
            # token 预算裁剪（从尾部去掉超预算的记忆）
            total = 0
            kept = []
            for content in selected:
                tokens = estimate_tokens(content)
                if total + tokens > self.settings.memory_max_tokens:
                    break
                kept.append(content)
                total += tokens
            return kept
        except Exception as exc:
            logger.warning("记忆召回失败：%s", exc)
            return []

    def _memory_candidates(
        self,
        question: str,
        memories: list[dict],
    ) -> list[dict]:
        """粗筛候选记忆：关键词 n-gram 命中 + 最近 N 条补足，控制在预算内。"""
        limit = self.settings.memory_candidate_limit
        recent = self.settings.memory_recent_fallback
        ngrams = self._question_ngrams(question)
        selected: list[dict] = []
        seen: set[int] = set()
        if ngrams:
            for memory in memories:
                if len(selected) >= limit:
                    break
                content = memory.get("content") or ""
                if any(ng in content for ng in ngrams) and memory["id"] not in seen:
                    seen.add(memory["id"])
                    selected.append(memory)
        # 最近记忆补足候选（list_memories 已按更新时间倒序）
        for memory in memories[:recent]:
            if len(selected) >= limit:
                break
            if memory["id"] not in seen:
                seen.add(memory["id"])
                selected.append(memory)
        return selected

    @staticmethod
    def _question_ngrams(question: str, max_n: int = 4) -> list[str]:
        """把问题拆成 2~4 字 n-gram，用于记忆内容粗筛（无 jieba 依赖）。"""
        q = re.sub(
            r"[\s，。！？、；：,.!?;:'\"“”‘’（）()《》<>\[\]{}|\\/]",
            "",
            question.lower(),
        )
        if len(q) <= 2:
            return [q] if q else []
        stop = set("的了是在我有你我他要这和也就不说一个会到与及")
        grams: set[str] = set()
        for n in range(2, max_n + 1):
            if len(q) < n:
                continue
            for i in range(len(q) - n + 1):
                gram = q[i : i + n]
                if any(ch not in stop for ch in gram):
                    grams.add(gram)
        return sorted(grams)[:60]

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
1. 只提取稳定、可复用的个人信息，忽略一次性问题和闲聊内容；
2. 每条用一句话、第三人称客观描述，不要推测；
3. 如果用户在纠正或覆盖之前的信息（例如"我不住北京了"），
   提取为描述"当前状态"的新事实，方便后续整理时覆盖旧记忆；
{force_note}
4. 输出 JSON 数组，元素形如 {{"category": "profile", "content": "..."}}，
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
            content = (resp.content or "").strip()
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
        """按分类去重后写入记忆库，返回新增条数。"""
        existing = repo.list_memories(db, limit=1000)
        existing_texts = [m["content"] for m in existing]
        added = 0
        for fact in facts:
            content = fact["content"]
            category = fact.get("category", "other")
            if self._is_duplicate(content, existing_texts, embeddings):
                continue
            repo.add_memory(db, content, category=category, source_conversation_id=source_conversation_id)
            existing_texts.append(content)
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
            < timedelta(hours=self.settings.memory_consolidate_interval_hours)
        ):
            return None
        active = [m for m in repo.list_memories(db, limit=2000)]
        if len(active) < self.settings.memory_consolidate_threshold:
            return None
        return self.consolidate_memories(db)

    def consolidate_memories(self, db: Session) -> dict:
        """整合整理：合并重复、新事实覆盖旧事实、归档过时项，并生成画像摘要。"""
        with _consolidate_lock:
            memories = repo.list_all_memories(db, limit=2000)
            active = [m for m in memories if m["status"] == "active"]
            if not active:
                return {"merged": 0, "archived": 0, "summary": None}

            lines = "\n".join(
                f"{m['id']}: [{m['category']}] {m['content']}"
                for m in active
            )
            prompt = f"""以下是系统长期记忆库中的全部活跃记忆（id: [分类] 内容）。
请做一次记忆整合整理：
1. 合并内容重复或高度相似的多条记忆为一条；
2. 当新记忆与旧记忆矛盾时（旧记忆在前、新记忆在后），以较新的内容为准，
   把过时/被覆盖的旧记忆 id 放入 archived_ids；
3. 保留仍有价值的分类与内容，删除琐碎无用的；
4. 生成一份不超过 150 字的"用户画像摘要"（第三人称概括用户身份、喜好、项目）。

输出 JSON（不要任何解释）：
{{
  "facts": [{{"source_ids": [多个原id], "category": "profile", "content": "整合后内容"}}],
  "archived_ids": [被归档的旧id],
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
                    content = (resp.content or "").strip()
                    match = re.search(r"\{.*\}", content, re.DOTALL)
                    if not match:
                        return {"merged": 0, "archived": 0, "summary": None}
                    data = json.loads(match.group(0))
                except Exception as exc:
                    logger.warning("记忆整合失败：%s", exc)
                    return {"merged": 0, "archived": 0, "summary": None}

            archived_ids = set(int(x) for x in (data.get("archived_ids") or []))
            for m in active:
                if m["id"] in archived_ids:
                    repo.update_memory(db, m["id"], status="archived")

            merged_count = 0
            actually_archived = set(archived_ids)
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
                # 保留最新的那条作为主记录，其余归档
                primary = max(source_ids)
                repo.update_memory(db, primary, content=new_content, category=category)
                for sid in source_ids:
                    if sid != primary:
                        repo.update_memory(db, sid, status="archived")
                        actually_archived.add(sid)
                merged_count += 1

            summary = str(data.get("summary", "")).strip()
            if summary:
                repo.set_meta(db, "memory_summary", summary)
            repo.set_meta(db, "memory_last_consolidated_at", datetime.now().isoformat())
            return {
                "merged": merged_count,
                "archived": len(actually_archived),
                "summary": summary or None,
            }

    @staticmethod
    def _is_duplicate(fact: str, existing_texts: list[str], embeddings) -> bool:
        for old in existing_texts:
            if fact == old:
                return True
        if not existing_texts:
            return False
        try:
            v1 = embeddings.embed_query(fact)
            v2s = embeddings.embed_documents(existing_texts[-10:], batch_size=16)
            for v2 in v2s:
                sim = sum(a * b for a, b in zip(v1, v2))
                if sim > 0.95:
                    return True
        except Exception:
            pass
        return False

    # ---------------- 任务规划 ----------------

    def plan(self, question: str) -> list[str]:
        """复杂问题生成 2~5 步执行计划；简单问题返回 []。"""
        if not self.settings.planner_enabled or not self._looks_complex(question):
            return []
        prompt = f"""用户提出了一个问题。判断它是否需要多步骤处理
（例如：需要检索多个资料、联网搜索+推理、对比分析、整理综述）。
如果需要，输出 2~5 步执行计划；如果一步即可完成，输出空数组。
要求：
1. 每步一句话，可包含建议手段（如"检索知识库""联网搜索""推理总结"）。
2. 当问题涉及书籍/文档（如询问某本书讲了什么）时，第一步必须是"检索知识库"，
   因为用户的知识库中可能已有该文档；联网搜索只能作为补充手段。
3. 只输出 JSON 字符串数组，不要任何解释。

问题：{question[:1000]}"""
        # 优先结构化输出（steps 由 schema 约束为数组，杜绝嵌套/多余文本解析问题）
        try:
            out = self._structured(PlanSteps, prompt)
            if out is not None and getattr(out, "steps", None) is not None:
                steps = [str(x).strip() for x in out.steps if str(x).strip()]
                return steps[:5]
        except Exception as exc:
            logger.warning("结构化规划失败，回退正则解析：%s", exc)
        # 回退：正文 JSON + 正则抽取（旧路径）
        try:
            resp = self._invoke([HumanMessage(content=prompt)])
            content = (resp.content or "").strip()
            match = re.search(r"\[.*\]", content, re.DOTALL)
            if not match:
                return []
            data = json.loads(match.group(0))
            steps = [str(x).strip() for x in data if str(x).strip()]
            return steps[:5]
        except Exception as exc:
            logger.warning("任务规划失败：%s", exc)
            return []

    @staticmethod
    def _looks_complex(question: str) -> bool:
        if len(question) >= 30:
            return True
        keywords = ("总结", "分析", "对比", "计划", "步骤", "方案", "优缺点", "整理", "综述")
        return any(k in question for k in keywords)
