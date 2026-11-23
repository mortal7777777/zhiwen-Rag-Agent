"""原生 checkpointer 的安全序列化：原生类型保真、真正不可序列化的才占位替换。

回归背景（2026-09-29 实测）：早期实现无条件先 _sanitize，把 langgraph 的
Interrupt 也换成 {"__unserializable__": "Interrupt"}，挂起后
`Command(resume=...)` 恢复时框架读 `value[0].id` 直接 AttributeError。
"""

from __future__ import annotations

import threading

from langgraph.types import Interrupt

from app.native_checkpoint import SafeJsonPlusSerializer, build_saver


def test_interrupt_roundtrips_untouched():
    serde = SafeJsonPlusSerializer()
    payload = {"kind": "approval", "ops": [{"call_id": "c1"}]}
    item = Interrupt(value=payload, id="abc123")

    typ, data = serde.dumps_typed([item])
    back = serde.loads_typed((typ, data))

    assert back[0].value == payload
    assert back[0].id == "abc123"


def test_unserializable_falls_back_to_placeholder():
    serde = SafeJsonPlusSerializer()
    typ, data = serde.dumps_typed({"evt": threading.Event()})
    back = serde.loads_typed((typ, data))

    assert back["evt"] == {"__unserializable__": "Event"}


def test_build_saver_enables_wal_and_busy_timeout(tmp_path):
    """并行 Send / 多会话共用一条连接：WAL + busy_timeout 是并发安全前提。"""
    saver, conn = build_saver(tmp_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        # 裸 conn 维护操作要借用 saver 的内部锁
        assert hasattr(saver, "lock")
    finally:
        conn.close()
