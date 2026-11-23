"""方案 A（2026-11-19）：客户端断开不再中止整轮生成。

语义：
- 断连后 `_ACTIVE_CONVS` 闸门保持占用（防并发写检查点）且 stop_event 不被置位；
- 运行的终止信号（done 事件 / None 哨兵，worker 线程必发其一）释放闸门，
  与 SSE 连接是否存活无关；
- /agent/cancel 仍可显式停止断连后的运行（前端「停止」与 CLI 同款）。
"""

from __future__ import annotations

import json
import threading

import anyio

from app.api.agent import agent_stream, is_conversation_active, request_cancel
from app.schemas import AgentChatRequest


class _LongRunService:
    """桥接模式假服务：session → 阻塞等闸门/stop → done。

    stop_seen 记录 stop_event 是否被置位（验证断连不再自动停止）。
    """

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.stop_seen = threading.Event()

    def run(self, **kwargs):
        stop = kwargs.get("stop_event")
        yield {"event": "session", "data": {"conversation_id": 77}}
        while not self.gate.is_set():
            if stop is not None and stop.is_set():
                self.stop_seen.set()
                break
            self.gate.wait(0.02)
        # cancel 置位也走 done 收尾（模拟图路由的 finalize 出口）
        if stop is not None and stop.is_set():
            self.stop_seen.set()
        yield {"event": "done", "data": {}}


async def _consume_session(resp):
    gen = resp.body_iterator
    first = json.loads((await gen.__anext__()).strip()[6:])
    assert first["event"] == "session"
    return gen, first["data"]["run_id"]


async def _wait_gate_released(cid: int, timeout_s: float = 5.0) -> None:
    for _ in range(int(timeout_s / 0.05)):
        if not is_conversation_active(cid):
            return
        await anyio.sleep(0.05)
    assert not is_conversation_active(cid), "闸门在运行结束后未释放"


def test_disconnect_keeps_gate_and_run_alive():
    """断连后闸门保持占用、stop_event 不被置位；运行结束才释放闸门。"""

    async def scenario():
        req = AgentChatRequest(question="断连语义", conversation_id=77)
        svc = _LongRunService()
        resp = await agent_stream(req, svc)
        gen, _ = await _consume_session(resp)

        # 客户端断开（刷新页面）：直接关闭 SSE 生成器
        await gen.aclose()

        # 方案 A：闸门仍占用 + 后台运行未被停止
        assert is_conversation_active(77) is True
        assert svc.stop_seen.is_set() is False

        # 运行跑完（worker 线程发 done → _enqueue 释放闸门），与连接无关
        svc.gate.set()
        await _wait_gate_released(77)
        assert svc.stop_seen.is_set() is False  # 全程无人取消，运行自然结束

    anyio.run(scenario)


def test_cancel_still_works_after_disconnect():
    """断连后「停止」按钮/CLI 的 cancel 端点仍能显式中止后台运行。"""

    async def scenario():
        req = AgentChatRequest(question="断连后取消", conversation_id=88)
        svc = _LongRunService()
        resp = await agent_stream(req, svc)
        gen, run_id = await _consume_session(resp)
        await gen.aclose()
        assert is_conversation_active(88) is True

        cancelled, reason = request_cancel(run_id)
        assert cancelled is True and reason == ""
        await _wait_gate_released(88)
        # 运行确实看到了 stop 信号（而不是断连自动置位）
        assert svc.stop_seen.is_set() is True

    anyio.run(scenario)


def test_disconnect_does_not_block_next_run_after_completion():
    """断连的运行结束后，新请求可以正常登记（闸门不残留）。"""

    async def scenario():
        req = AgentChatRequest(question="闸门释放后续跑", conversation_id=99)
        svc = _LongRunService()
        resp = await agent_stream(req, svc)
        gen, _ = await _consume_session(resp)
        await gen.aclose()
        svc.gate.set()
        await _wait_gate_released(99)

        # 新运行可正常登记（若断连路径泄漏注册表，这里会 409）
        svc2 = _LongRunService()
        resp2 = await agent_stream(req, svc2)
        gen2, _ = await _consume_session(resp2)
        assert is_conversation_active(99) is True
        svc2.gate.set()
        async for _ in gen2:
            pass
        await gen2.aclose()
        await _wait_gate_released(99)

    anyio.run(scenario)
