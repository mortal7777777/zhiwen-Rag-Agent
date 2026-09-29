"""增量流式：正文边生成边推送；工具轮的过渡思考文本整轮撤回。

协议（向后兼容，新增事件名）：
- stream_delta：按 chunk 推送正文增量（含工具轮的过渡文本）
- stream_end {keep}：keep=true 表示增量文本即最终正文（前端跳过随后的全文 token）；
  keep=false 表示工具轮/异常轮，前端撤回本轮已推送的字符
- token：全文事件保持不变，供 CLI / run_json / 评测脚本消费
"""

from __future__ import annotations

import queue
import time
from types import SimpleNamespace

from langchain_core.messages import AIMessageChunk

from app.agent.nodes.agent import _agent_node
from app.agent.state import EventBus
from _node_rt import runtime_from_state  # noqa: E402


class _FakeRag:
    def acquire_llm(self):
        pass

    def release_llm(self):
        pass


class _FakeChat:
    def __init__(self, tool_calls=(), texts=(), delays=None):
        self._tool_calls = list(tool_calls)
        self._texts = list(texts)
        self._delays = list(delays) if delays else [0.0] * max(1, len(self._texts))

    def bind_tools(self, tools):
        return self

    def stream(self, messages, config=None):
        calls, texts, delays = self._tool_calls, self._texts, self._delays

        def gen():
            for i, delay in enumerate(delays):
                if delay:
                    time.sleep(delay)
                text = texts[i] if i < len(texts) else ""
                # 只有最后一个 chunk 携带 tool_calls（贴近真实供应商行为）
                yield AIMessageChunk(
                    content=text,
                    tool_calls=calls if i == len(delays) - 1 else [],
                )

        return gen()


def _service(chat, stall_s=0.4):
    settings = SimpleNamespace(
        agent_max_iterations=12,
        agent_max_dispatch_rounds=3,
        agent_llm_stall_timeout_s=stall_s,
    )
    return SimpleNamespace(
        chat=chat,
        chat_plain=_FakeChat(texts=["正文"]),
        settings=settings,
        rag=_FakeRag(),
    )


def _state(service, bus):
    return {
        "service": service,
        "bus": bus,
        "runtime": {"started": time.perf_counter(), "final_text": ""},
        "messages": [],
        "tools": [object()],
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
        "dispatch_rounds": 0,
    }


def _events(bus):
    out = []
    while not bus._queue.empty():
        out.append(bus._queue.get_nowait())
    return out


def test_answer_round_streams_deltas_then_keeps():
    bus = EventBus(queue.Queue())
    chat = _FakeChat(texts=["你好", "，世界"])
    state = _state(_service(chat), bus)

    _agent_node(state, runtime_from_state(state))
    events = _events(bus)

    deltas = "".join(e["data"] for e in events if e["event"] == "stream_delta")
    assert deltas == "你好，世界"
    kinds = [e["event"] for e in events]
    assert kinds.count("stream_end") == 1
    end = next(e for e in events if e["event"] == "stream_end")
    assert end["data"] == {"keep": True}
    # 全文 token 仍要发（CLI/评测依赖），且排在 stream_end 之后 → 前端可跳过
    assert kinds.index("stream_end") < kinds.index("token")
    token = next(e for e in events if e["event"] == "token")
    assert token["data"] == "你好，世界"
    assert state["runtime"]["final_text"] == "你好，世界"


def test_tool_round_streams_then_retracts():
    bus = EventBus(queue.Queue())
    calls = [
        {"name": "read_file", "args": {"path": "x"}, "id": "c1", "type": "tool_call"}
    ]
    chat = _FakeChat(tool_calls=calls, texts=["我先读一下文件 "])
    state = _state(_service(chat), bus)

    out = _agent_node(state, runtime_from_state(state))
    events = _events(bus)

    # 增量先推、随整轮撤回；过渡文本既不上屏（撤回后）也不进最终回答
    stream_events = [
        e for e in events if e["event"] in ("stream_delta", "stream_end", "token")
    ]
    assert [e["event"] for e in stream_events] == ["stream_delta", "stream_end"]
    assert stream_events[0]["data"] == "我先读一下文件 "
    assert stream_events[1]["data"] == {"keep": False}
    assert not any(e["event"] == "token" for e in events)
    assert state["runtime"]["final_text"] == ""
    assert out["pending_tool_calls"] and out["pending_tool_calls"][0]["name"] == "read_file"


def test_large_chunks_flush_by_window():
    """聚合窗口按字符阈值触发：约 120 字一块，避免数百个碎 SSE 帧。"""
    bus = EventBus(queue.Queue())
    chat = _FakeChat(texts=["A" * 130, "B"])
    state = _state(_service(chat), bus)

    _agent_node(state, runtime_from_state(state))
    events = _events(bus)
    deltas = [e["data"] for e in events if e["event"] == "stream_delta"]

    assert deltas[0] == "A" * 130  # 达到阈值即刻发出，不等轮结束
    assert "".join(deltas) == "A" * 130 + "B"  # 末尾残留由收尾补发
    assert len(deltas) == 2


def test_stall_after_partial_output_retracts():
    bus = EventBus(queue.Queue())
    # 第一个 chunk 正常到达（已推送），第二个超过卡死阈值 → 整轮撤回 + 报错
    chat = _FakeChat(texts=["半截回答", "不应到达"], delays=[0.05, 5.0])
    state = _state(_service(chat, stall_s=0.4), bus)

    _agent_node(state, runtime_from_state(state))
    events = _events(bus)

    assert state["runtime"]["status"] == "error"
    assert "卡死" in state["runtime"]["error"]
    ends = [e for e in events if e["event"] == "stream_end"]
    assert ends and ends[-1]["data"] == {"keep": False}
    assert not any(e["event"] == "token" for e in events)
