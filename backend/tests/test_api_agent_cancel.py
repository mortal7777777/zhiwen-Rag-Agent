"""取消接口测试：run_id 登记 + /agent/cancel 停止流式生成（无真实网络）。"""

from __future__ import annotations

import json
import threading

import anyio

from app.api.agent import (
    agent_cancel,
    agent_stream,
    register_active_run,
    request_cancel,
    unregister_active_run,
)
from app.schemas import AgentChatRequest


class DummyService:
    """run() 在 session 后等待 stop_event，模拟长时间生成。"""

    def run(self, **kwargs):
        stop = kwargs.get("stop_event")
        yield {"event": "session", "data": {"conversation_id": 7}}
        if stop is not None:
            stop.wait(timeout=5)
        yield {"event": "done", "data": {"ok": True, "stopped": True}}


def test_register_and_cancel_sets_event():
    stop = threading.Event()
    run_id = register_active_run(stop)
    try:
        assert not stop.is_set()
        assert request_cancel(run_id) == (True, "")
        assert stop.is_set()
        assert request_cancel(run_id) == (True, "")  # 幂等
    finally:
        unregister_active_run(run_id)
    assert request_cancel(run_id) == (False, "run not found")


def test_cancel_unknown_run_is_graceful():
    result = anyio.run(agent_cancel, "no-such-run")
    assert result["cancelled"] is False
    assert result["reason"] == "run not found"


def test_stream_injects_run_id_and_cancel_stops_generation():
    """逐帧驱动 SSE 生成器：session 带 run_id，cancel 后立刻收尾并清理注册表。"""
    captured: list[str] = []

    async def scenario() -> None:
        response = await agent_stream(
            AgentChatRequest(question="测试问题"),
            DummyService(),
        )
        gen = response.body_iterator
        first = await gen.__anext__()
        event = json.loads(first.strip()[6:])
        assert event["event"] == "session"
        run_id = event["data"].get("run_id")
        assert run_id
        captured.append(run_id)
        assert request_cancel(run_id) == (True, "")

        frames = [first]
        async for frame in gen:
            frames.append(frame)
        joined = "".join(frames)
        assert '"done"' in joined
        assert '"stopped": true' in joined

    anyio.run(scenario)
    # 流结束后注册表已清理
    assert captured
    assert request_cancel(captured[0]) == (False, "run not found")
