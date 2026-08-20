"""轻量会话 checkpoint：工具轮后保存可序列化状态，异常中断后可恢复。

用 SQLite（stdlib）持久化，不依赖 LangGraph saver，重启进程后依然有效。
流程：prepare 时若发现 pending 快照 → 恢复消息链继续执行；
finalize 成功/停止后清除快照；异常退出保留快照供下次恢复。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

_lock = threading.Lock()


def _dump_messages(messages: list) -> list:
    """手动序列化消息（不依赖 LangChain beta 的 load API）。"""
    out: list[dict] = []
    for m in messages:
        try:
            if isinstance(m, SystemMessage):
                out.append({"t": "system", "c": m.content})
            elif isinstance(m, HumanMessage):
                out.append({"t": "human", "c": m.content})
            elif isinstance(m, AIMessage):
                # DeepSeek 思考模式要求带 tool_calls 的 assistant 消息
                # 原样回传 reasoning_content，否则 API 报 400；
                # checkpoint 必须保留它，否则恢复后的请求会失败
                out.append(
                    {
                        "t": "ai",
                        "c": m.content,
                        "tool_calls": list(m.tool_calls or []),
                        "reasoning": (
                            getattr(m, "additional_kwargs", {}) or {}
                        ).get("reasoning_content"),
                    }
                )
            elif isinstance(m, ToolMessage):
                out.append(
                    {
                        "t": "tool",
                        "c": m.content,
                        "name": m.name,
                        "id": m.tool_call_id,
                    }
                )
        except Exception:
            continue
    return out


def _load_messages(data: list) -> list:
    messages = []
    for item in data:
        try:
            t = item.get("t")
            if t == "system":
                messages.append(SystemMessage(content=item["c"]))
            elif t == "human":
                messages.append(HumanMessage(content=item["c"]))
            elif t == "ai":
                reasoning = item.get("reasoning")
                messages.append(
                    AIMessage(
                        content=item.get("c", ""),
                        tool_calls=item.get("tool_calls") or [],
                        # 无条件回传（含空串），旧快照缺字段时补空串
                        additional_kwargs=(
                            {
                                "reasoning_content": (
                                    reasoning if reasoning is not None else ""
                                )
                            }
                            if item.get("tool_calls")
                            else {}
                        ),
                    )
                )
            elif t == "tool":
                messages.append(
                    ToolMessage(
                        content=item.get("c", ""),
                        name=item.get("name"),
                        tool_call_id=item.get("id") or "",
                    )
                )
        except Exception:
            continue
    return messages


class CheckpointStore:
    """基于 SQLite 的 checkpoint 存储。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                conversation_id INTEGER PRIMARY KEY,
                state_json TEXT NOT NULL,
                pending INTEGER DEFAULT 1,
                updated_at REAL
            )
            """
        )
        self._conn.commit()

    def save(self, conversation_id: int, payload: dict) -> None:
        with _lock:
            state = {
                **payload,
                "messages": _dump_messages(payload.get("messages") or []),
                "saved_at": time.time(),
            }
            self._conn.execute(
                """
                INSERT OR REPLACE INTO checkpoints (conversation_id, state_json, pending, updated_at)
                VALUES (?, ?, 1, ?)
                """,
                (
                    conversation_id,
                    json.dumps(state, ensure_ascii=False),
                    time.time(),
                ),
            )
            self._conn.commit()

    def load(self, conversation_id: int) -> dict | None:
        with _lock:
            row = self._conn.execute(
                "SELECT state_json, pending FROM checkpoints WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        state, pending = row
        try:
            data = json.loads(state)
        except Exception:
            return None
        data["pending"] = bool(pending)
        data["messages"] = _load_messages(data.get("messages") or [])
        return data

    def clear(self, conversation_id: int) -> None:
        with _lock:
            self._conn.execute(
                "DELETE FROM checkpoints WHERE conversation_id = ?",
                (conversation_id,),
            )
            self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


_store: CheckpointStore | None = None


def get_store(settings) -> CheckpointStore:
    """全局单例 checkpoint 存储（opensearch_meta/checkpoints.db）。"""
    global _store
    if _store is None:
        _store = CheckpointStore(settings.meta_dir / "checkpoints.db")
    return _store
