"""上下文服务 · 基座（从 agent/context.py 拆出，2026-10-10）。

低温模型调用封装（_invoke/_structured）、token 估算与窗口预算工具，
以及历史预算方法 history_budget_for / _clamp_to_context_window。
mixins 只允许依赖本模块。"""


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

def estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文约 1.5 字符/token，英文/数字约 4 字符/token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return max(1, int(cjk / 1.5 + other / 4))


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


class ContextServiceBase:

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
