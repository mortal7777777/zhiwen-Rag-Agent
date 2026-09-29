"""LangGraph 原生 checkpointer（SqliteSaver + 安全序列化）。

用途：
- 每个 superstep 自动保存快照（时间旅行/审计）；
- 与自写 checkpoint.py 互补：自写快照负责跨轮中断恢复，原生 saver 负责
  “查看任意历史快照”的审计能力；
- 序列化时把 Session/EventBus/线程/工具等不可序列化对象替换为占位符，
  LangChain 消息与业务字段（todos/plan/sources/tool_trace）正常保留。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

logger = logging.getLogger(__name__)

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
    """jsonplus 序列化前先替换不可序列化对象，避免整个 checkpoint 保存失败。

    注意顺序：**先走原生 jsonplus**（它认识 LangChain 消息、langgraph 的
    Interrupt/Send 等类型），只有原生失败才做占位替换。早期实现无条件先
    _sanitize，把 Interrupt 也换成了 {"__unserializable__": "Interrupt"}——
    挂起后恢复时框架读 pending 写的 Interrupt.id 直接 AttributeError
    （2026-09-29 实测）。
    """

    def dumps_typed(self, obj):
        try:
            return super().dumps_typed(obj)
        except Exception:
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


def prune_thread(saver, thread_id: str, keep: int = 20) -> int:
    """裁剪某线程的旧检查点：只保留最近 keep 个快照（连同其写入）。

    原生 saver 每个 superstep 存一条含完整 messages 的快照，长会话会让
    checkpoints.sqlite 单调增长（实测 700MB+）。收尾（finalize）后调用；
    此时线程已无未完成任务，裁剪不会碰到挂起/中断中的状态。
    删除的行不回收磁盘空间（需要 VACUUM，见 prune_all）。
    """
    conn = getattr(saver, "conn", None)
    if conn is None or keep <= 0:
        return 0
    try:
        rows = conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = ? "
            "ORDER BY checkpoint_id DESC LIMIT -1 OFFSET ?",
            (thread_id, keep),
        ).fetchall()
        stale = [r[0] for r in rows]
        if not stale:
            return 0
        conn.executemany(
            "DELETE FROM writes WHERE thread_id = ? AND checkpoint_id = ?",
            [(thread_id, cid) for cid in stale],
        )
        conn.executemany(
            "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_id = ?",
            [(thread_id, cid) for cid in stale],
        )
        conn.commit()
        return len(stale)
    except Exception as exc:
        logger.warning("裁剪检查点失败（thread=%s）：%s", thread_id, exc)
        return 0


def prune_all(saver, keep: int = 20) -> dict:
    """全库裁剪：每个线程保留最近 keep 条（维护脚本用）。"""
    conn = getattr(saver, "conn", None)
    if conn is None:
        return {"threads": 0, "deleted": 0}
    threads = [
        r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
    ]
    deleted = 0
    for tid in threads:
        deleted += prune_thread(saver, tid, keep)
    return {"threads": len(threads), "deleted": deleted}


def vacuum(saver) -> None:
    """回收 sqlite 空间：删除大量行后文件不会自动缩小。"""
    conn = getattr(saver, "conn", None)
    if conn is None:
        return
    try:
        conn.execute("VACUUM")
    except Exception as exc:
        logger.warning("VACUUM 失败：%s", exc)


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
                # 无条件写入（含空串）：工具轮消息必须回传 reasoning_content
                reasoning = (getattr(m, "additional_kwargs", {}) or {}).get(
                    "reasoning_content"
                )
                marker["__reasoning__"] = (
                    reasoning if reasoning is not None else ""
                )
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
