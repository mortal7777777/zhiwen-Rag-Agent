"""空正文兜底：思考吃光输出预算时不能静默返回空回答（2026-09-24 实测）。

覆盖三件事：判定纯函数、重试轮切无思考模型、finalize 把空正文降级为明确错误。
"""

import queue
import time
from types import SimpleNamespace

from langchain_core.messages import AIMessageChunk

from app.agent.nodes.agent import _agent_node, _should_retry_empty
from app.agent.nodes.finalize import _finalize_node
from app.agent.state import EventBus


# ---------------- 判定纯函数 ----------------


def _kwargs(**over):
    base = dict(
        tool_calls=[],
        merged_text="",
        usage={"output_tokens": 32000},
        forced_final=False,
        status=None,
        retry_count=0,
    )
    base.update(over)
    return base


def test_allowed_when_budget_eaten_by_thinking():
    assert _should_retry_empty(**_kwargs()) is True


def test_blocked_when_text_present():
    assert _should_retry_empty(**_kwargs(merged_text="有正文")) is False


def test_blocked_without_output_tokens():
    assert _should_retry_empty(**_kwargs(usage={"output_tokens": 0})) is False
    assert _should_retry_empty(**_kwargs(usage=None)) is False


def test_blocked_at_limit_and_forced_final():
    assert _should_retry_empty(**_kwargs(retry_count=2)) is False
    assert _should_retry_empty(**_kwargs(forced_final=True)) is False
    assert _should_retry_empty(**_kwargs(status="stopped")) is False


def test_blocked_when_tool_calls_present():
    assert (
        _should_retry_empty(**_kwargs(tool_calls=[{"name": "read_file"}])) is False
    )


# ---------------- 节点级：重试轮切模型 + 返回状态 ----------------


class _FakeRag:
    def acquire_llm(self):
        pass

    def release_llm(self):
        pass


class _FakeChat:
    """固定吐一个 chunk；plain=True 时记录自己是被选中的无思考模型。"""

    def __init__(self, text="", out_tokens=0, planner=None):
        self.text = text
        self.out_tokens = out_tokens
        self.used = False

    def bind_tools(self, tools):
        return self

    def stream(self, messages, config=None):
        text, out_tokens = self.text, self.out_tokens
        self.used = True

        def gen():
            yield AIMessageChunk(
                content=text,
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": out_tokens,
                    "total_tokens": 10 + out_tokens,
                },
            )

        return gen()


def _service(chat, plain):
    settings = SimpleNamespace(
        agent_max_iterations=12,
        agent_llm_stall_timeout_s=5.0,
    )
    return SimpleNamespace(chat=chat, chat_plain=plain, settings=settings, rag=_FakeRag())


def _state(service, bus, **over):
    state = {
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
    state.update(over)
    return state


def test_empty_answer_triggers_retry_and_switches_model():
    bus = EventBus(queue.Queue())
    chat = _FakeChat(text="", out_tokens=32000)
    plain = _FakeChat(text="这是正文", out_tokens=20)
    state = _state(_service(chat, plain), bus)

    result = _agent_node(state)

    assert result["empty_retry_count"] == 1
    assert result["empty_retry_pending"] is True
    assert result["force_continue"] is True
    assert any(
        isinstance(m, object) and getattr(m, "type", "") == "system"
        and "没有产生任何正文" in str(getattr(m, "content", ""))
        for m in result["messages"]
    )


def test_retry_round_uses_no_thinking_model():
    bus = EventBus(queue.Queue())
    chat = _FakeChat(text="不应被使用", out_tokens=1)
    plain = _FakeChat(text="重试正文", out_tokens=5)
    state = _state(
        _service(chat, plain),
        bus,
        empty_retry_pending=True,
        empty_retry_count=1,
    )

    _agent_node(state)

    assert plain.used is True
    assert chat.used is False
    assert state["runtime"]["final_text"] == "重试正文"


# ---------------- finalize：空正文 → status=error + error 事件 ----------------


def test_finalize_empty_answer_marks_error():
    bus = EventBus(queue.Queue())
    service = SimpleNamespace(
        settings=SimpleNamespace(
            checkpoint_enabled=False,
            trajectory_compress_enabled=False,
        )
    )
    state = {
        "service": service,
        "bus": bus,
        "db": None,
        "runtime": {
            "started": time.perf_counter(),
            "final_text": "",
            "status": "ok",
        },
        "sources": [],
        "tool_trace": [],
        "question": "测试",
        "messages": [],
        "plan_steps": [],
        "todos": [],
    }

    _finalize_node(state)

    assert state["runtime"]["status"] == "error"
    events = []
    while not bus._queue.empty():
        events.append(bus._queue.get_nowait())
    assert any(
        ev["event"] == "error" and ev["data"].get("code") == "empty_answer"
        for ev in events
    )
