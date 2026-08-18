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

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
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


def messages_to_history_rows(messages: list) -> list[dict]:
    """把快照消息转成可写回数据库的历史行（含工具轮，缓存前缀保真）。

    - 工具轮 AIMessage（带 tool_calls）-> role='assistant'，
      content 为 {"__tool_calls__": [...]} 标记 JSON；
    - ToolMessage -> role='tool'，tool_trace 存配对元数据；
    - 与 langgraph_agent._rows_to_history 的格式互逆。
    """
    import json as _json

    rows: list[dict] = []
    for m in messages or []:
        if isinstance(m, HumanMessage):
            content = m.content
            if not isinstance(content, str):
                content = str(content)
            if content.strip():
                rows.append({"role": "user", "content": content})
        elif isinstance(m, AIMessage):
            calls = list(getattr(m, "tool_calls", None) or [])
            if calls:
                marker = {"__tool_calls__": calls}
                reasoning = (getattr(m, "additional_kwargs", {}) or {}).get(
                    "reasoning_content"
                )
                if reasoning:
                    marker["__reasoning__"] = reasoning
                rows.append(
                    {
                        "role": "assistant",
                        "content": _json.dumps(marker, ensure_ascii=False),
                    }
                )
                continue
            content = m.content
            if not isinstance(content, str):
                content = str(content)
            if content.strip():
                rows.append({"role": "assistant", "content": content})
        elif isinstance(m, ToolMessage):
            content = m.content
            if not isinstance(content, str):
                content = str(content)
            rows.append(
                {
                    "role": "tool",
                    "content": content,
                    "tool_trace": [
                        {
                            "tool_call_id": m.tool_call_id or "",
                            "name": m.name or "",
                        }
                    ],
                }
            )
    return rows
