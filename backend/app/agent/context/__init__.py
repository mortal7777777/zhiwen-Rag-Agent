"""上下文服务装配层（原 app/agent/context.py 包化，2026-10-10）。

ContextService = CompactMixin + MemoryMixin + ContextServiceBase 的薄装配；
原有的全部导入路径与命名空间属性保持不变（ContextService / MEMORY_CATEGORIES /
context_window_for / trim_history_for_budget / ConsolidateOutput 等）。
"""
from ...db import repository as repo  # 旧模块命名空间兼容：测试 monkeypatch context_mod.repo
from .base import ContextServiceBase, estimate_tokens, context_window_for, trim_history_for_budget
from .compact import CompactMixin
from .memory import (
    MEMORY_CATEGORIES,
    MEMORY_INTENT_PATTERNS,
    ConsolidateOutput,
    FactItem,
    FactsList,
    MergedFact,
    MemoryMixin,
    _memory_vec_cache,  # 旧模块命名空间兼容：测试夹具 clear 这个内存向量缓存
)


class ContextService(CompactMixin, MemoryMixin, ContextServiceBase):
    """把记忆 / 摘要 / 规划组装成分层上下文。"""


__all__ = [
    "ContextService",
    "MEMORY_CATEGORIES",
    "MEMORY_INTENT_PATTERNS",
    "ConsolidateOutput",
    "FactItem",
    "FactsList",
    "MergedFact",
    "estimate_tokens",
    "context_window_for",
    "trim_history_for_budget",
]
