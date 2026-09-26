"""流式看门狗：模型长时间不吐任何 chunk 时必须报错收尾，不能静默空回答。

2026-09-24 实测末次调用 606s 只产出 1 个 token，日志无完成记录，用户只看到
「没有回答」——httpx read timeout 限制的是单次 socket 读，SSE 有心跳就永不触发。
"""

import queue
import time
from types import SimpleNamespace

from langchain_core.messages import AIMessageChunk

from app.agent.nodes.agent import _agent_node, _stall_timeout
from app.agent.state import EventBus


class _FakeRag:
    def acquire_llm(self):
        pass

    def release_llm(self):
        pass


class _FakeChat:
    """按给定节奏吐 chunk；长 delay 用来模拟卡死。"""

    def __init__(self, delays, texts=None):
        self._delays = delays
        self._texts = texts or []

    def bind_tools(self, tools):
        return self

    def stream(self, messages, config=None):
        delays, texts = self._delays, self._texts

        def gen():
            for i, delay in enumerate(delays):
                time.sleep(delay)
                yield AIMessageChunk(
                    content=texts[i] if i < len(texts) else ""
                )

        return gen()


def _service(chat, stall_s=0.4):
    settings = SimpleNamespace(
        agent_max_iterations=12,
        agent_llm_stall_timeout_s=stall_s,
    )
    return SimpleNamespace(chat=chat, settings=settings, rag=_FakeRag())


def _state(service, bus):
    return {
        "service": service,
        "bus": bus,
        "runtime": {"started": time.perf_counter(), "final_text": ""},
        "messages": [],
        "tools": [],
        "tool_calls_used": 0,
        "forced_final": False,
        "last_call_warned": False,
        "todos": [],
        "plan_map": [],
        "plan_done_count": 0,
        "plan_push_count": 0,
        "pending_tool_calls": [],
        "stop_event": None,
        "question": "测试",
    }


def _events(bus):
    out = []
    while not bus._queue.empty():
        out.append(bus._queue.get_nowait())
    return out


def test_stall_emits_error_and_sets_status():
    bus = EventBus(queue.Queue())
    state = _state(_service(_FakeChat(delays=[5.0])), bus)
    started = time.perf_counter()
    result = _agent_node(state)
    elapsed = time.perf_counter() - started

    assert elapsed < 3.0, "看门狗应在阈值附近收尾，而不是等流自己结束"
    assert state["runtime"]["status"] == "error"
    assert "卡死" in state["runtime"]["error"]
    assert result["pending_tool_calls"] == []
    assert any(
        ev["event"] == "error" and ev["data"].get("code") == "llm_stall"
        for ev in _events(bus)
    )


def test_no_false_positive_when_chunks_flow():
    bus = EventBus(queue.Queue())
    state = _state(
        _service(_FakeChat(delays=[0.05] * 4, texts=["你", "好", "，", "世界"])),
        bus,
    )
    _agent_node(state)

    assert state["runtime"].get("status") != "error"
    assert state["runtime"]["final_text"] == "你好，世界"


def test_stall_timeout_defaults_and_override():
    assert _stall_timeout(SimpleNamespace(agent_llm_stall_timeout_s=12)) == 12.0
    assert _stall_timeout(SimpleNamespace()) == 150.0
    assert _stall_timeout(SimpleNamespace(agent_llm_stall_timeout_s=0)) == 150.0
    assert _stall_timeout(SimpleNamespace(agent_llm_stall_timeout_s="bad")) == 150.0
