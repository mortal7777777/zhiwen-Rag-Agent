"""上下文服务 · 会话滚动摘要（从 agent/context.py 拆出，2026-10-10）。

压缩编排（compact_conversation）、占用统计（context_stats）、手动压缩与
摘要生成（_summarize）。"""


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

from ...config import Settings
from ...db import repository as repo
from ...runtime_config import effective
from ...llm_text import message_text
from ...tracing import get_aux_usage_collector

logger = logging.getLogger(__name__)

from .base import estimate_tokens, trim_history_for_budget

class CompactMixin:

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
