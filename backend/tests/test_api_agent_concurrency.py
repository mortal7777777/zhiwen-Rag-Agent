"""会话并发闸门：检查与登记必须原子，非流式端点也要占住会话。

修复背景（2026-10-23）：
1. `/agent/chat` 只做 `_reject_if_busy` 检查、从不登记 `_ACTIVE_CONVS`，
   运行期间流式请求可以并发写同一个检查点线程；
2. 流式端点的检查与登记隔着一次异步返回（登记发生在生成器迭代时），
   两个几乎同时到达的请求可能都通过检查。
"""

from __future__ import annotations

import json
import threading

import anyio
import pytest
from fastapi import HTTPException

from app.api.agent import (
    agent_chat,
    agent_stream,
    is_conversation_active,
    request_cancel,
    try_register_active_run,
    unregister_active_run,
)
from app.schemas import AgentChatRequest


class _StreamService:
    def run(self, **kwargs):
        stop = kwargs.get("stop_event")
        yield {"event": "session", "data": {"conversation_id": 7}}
        if stop is not None:
            stop.wait(timeout=5)
        yield {"event": "done", "data": {"ok": True, "stopped": True}}


class _ChatService:
    def __init__(self):
        self.active_inside = None

    def run_json(self, **kwargs):
        self.active_inside = is_conversation_active(kwargs.get("conversation_id"))
        return {"answer": "ok"}


def test_try_register_is_exclusive_per_conversation():
    rid = try_register_active_run(threading.Event(), conversation_id=42)
    assert rid is not None
    try:
        assert try_register_active_run(threading.Event(), 42) is None
        other = try_register_active_run(threading.Event(), 43)
        assert other is not None
        unregister_active_run(other)
    finally:
        unregister_active_run(rid)
    # 注销后可以重新登记
    again = try_register_active_run(threading.Event(), 42)
    assert again is not None
    unregister_active_run(again)


def test_second_stream_for_same_conversation_is_rejected():
    """两个请求都通过端点早期检查后，生成器层仍只放行一个。"""

    async def scenario():
        req = AgentChatRequest(question="并发", conversation_id=7)
        resp1 = await agent_stream(req, _StreamService())
        resp2 = await agent_stream(req, _StreamService())
        gen1, gen2 = resp1.body_iterator, resp2.body_iterator

        first = json.loads((await gen1.__anext__()).strip()[6:])
        assert first["event"] == "session"
        run_id = first["data"]["run_id"]

        busy = json.loads((await gen2.__anext__()).strip()[6:])
        assert busy["event"] == "error"
        assert busy["data"]["code"] == "conversation_busy"

        assert request_cancel(run_id) == (True, "")
        async for _ in gen1:
            pass
        await gen1.aclose()
        await gen2.aclose()

    anyio.run(scenario)
    assert not is_conversation_active(7)


def test_chat_endpoint_registers_and_releases_conversation():
    service = _ChatService()
    agent_chat(AgentChatRequest(question="q", conversation_id=9), service)
    assert service.active_inside is True
    assert not is_conversation_active(9)


def test_chat_endpoint_rejected_when_conversation_active():
    rid = try_register_active_run(threading.Event(), conversation_id=11)
    assert rid is not None
    try:
        with pytest.raises(HTTPException) as exc:
            agent_chat(
                AgentChatRequest(question="q", conversation_id=11),
                _ChatService(),
            )
        assert exc.value.status_code == 409
    finally:
        unregister_active_run(rid)
