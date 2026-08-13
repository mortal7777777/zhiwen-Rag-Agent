"""LangGraph 原生 checkpointer（SqliteSaver + 安全序列化）。

用途：
- 每个 superstep 自动保存快照（时间旅行/审计）；
- 与自写 checkpoint.py 互补：自写快照负责跨轮中断恢复，原生 saver 负责
  “查看任意历史快照”的审计能力；
- 序列化时把 Session/EventBus/线程/工具等不可序列化对象替换为占位符，
  LangChain 消息与业务字段（todos/plan/sources/tool_trace）正常保留。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

from langchain_core.messages import BaseMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

_SENTINEL = "__unserializable__"


def _sanitize(obj):
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, BaseMessage):
        return obj  # jsonplus 原生支持 LangChain 消息
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, tuple):
        return [_sanitize(x) for x in obj]
    if isinstance(obj, list):
        return [_sanitize(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _sanitize(v) for k, v in obj.items()}
    return {_SENTINEL: type(obj).__name__}


class SafeJsonPlusSerializer(JsonPlusSerializer):
    """jsonplus 序列化前先替换不可序列化对象，避免整个 checkpoint 保存失败。"""

    def dumps_typed(self, obj):
        return super().dumps_typed(_sanitize(obj))

    def loads_typed(self, data):
        return super().loads_typed(data)


def build_saver(meta_dir: Path):
    """创建 SqliteSaver（同步）；返回 (saver, connection)。"""
    meta_dir = Path(meta_dir)
    meta_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(meta_dir / "checkpoints.sqlite"), check_same_thread=False)
    saver = SqliteSaver(conn, serde=SafeJsonPlusSerializer())
    saver.setup()
    return saver, conn
