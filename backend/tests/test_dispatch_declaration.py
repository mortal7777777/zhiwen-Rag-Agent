"""声明式派发：主代理自己声明子任务 → 条件边扇出 → 结果以 ToolMessage 回填。

背景（2026-09-24 事故）：派发决定原本由 planner + 规则层做（信息最少的一层），
"编写 HTML 文件"被派给隔离上下文的子代理，它把磁盘上的旧文件当成果汇报，
主 agent 整轮没产出。现在改为主代理在运行中声明，工具调用形状 + 图边执行。
"""

from __future__ import annotations

import queue
import time
from types import SimpleNamespace

from langchain_core.messages import AIMessageChunk

from app.agent.graph import _route_after_agent
from app.agent.nodes.agent import _agent_node
from app.agent.nodes.subagent import (
    DISPATCH_TOOL_NAME,
    _build_context_pack,
    _dispatch_node,
)
from app.agent.state import EventBus


# ---------------- 条件边 ----------------


def test_route_prefers_dispatch_then_tools():
    assert (
        _route_after_agent({"pending_subtasks": [{"task": "A"}]}) == "dispatch"
    )
    assert (
        _route_after_agent(
            {"pending_subtasks": [{"task": "A"}], "pending_tool_calls": [{}]}
        )
        == "dispatch"
    )
    assert _route_after_agent({"pending_tool_calls": [{}]}) == "tools"
    assert _route_after_agent({"force_continue": True}) == "agent"
    assert _route_after_agent({}) == "finalize"


def test_route_stop_wins():
    ev = SimpleNamespace(is_set=lambda: True)
    assert _route_after_agent({"stop_event": ev, "pending_subtasks": [{}]}) == "finalize"


# ---------------- agent 节点解析声明 ----------------


class _FakeRag:
    def acquire_llm(self):
        pass

    def release_llm(self):
        pass


class _FakeChat:
    def __init__(self, tool_calls, text=""):
        self._tool_calls = tool_calls
        self._text = text

    def bind_tools(self, tools):
        return self

    def stream(self, messages, config=None):
        calls, text = self._tool_calls, self._text

        def gen():
            yield AIMessageChunk(content=text, tool_calls=list(calls))

        return gen()


def _service(tool_calls, text=""):
    settings = SimpleNamespace(
        agent_max_iterations=12,
        agent_llm_stall_timeout_s=5.0,
        agent_max_dispatch_rounds=3,
    )
    return SimpleNamespace(
        chat=_FakeChat(tool_calls, text),
        chat_plain=_FakeChat([], "正文"),
        settings=settings,
        rag=_FakeRag(),
    )


def _state(service, bus, **over):
    state = {
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
        "question": "写一个页面",
        "dispatch_rounds": 0,
    }
    state.update(over)
    return state


def _decl(task="写一个页面", mode="execute", call_id="c1"):
    return {
        "name": DISPATCH_TOOL_NAME,
        "args": {"task": task, "mode": mode},
        "id": call_id,
        "type": "tool_call",
    }


def test_agent_node_parses_declaration():
    bus = EventBus(queue.Queue())
    state = _state(_service([_decl()]), bus)
    out = _agent_node(state)

    assert [s["task"] for s in out["pending_subtasks"]] == ["写一个页面"]
    # 声明不进 tools 节点：否则会被当成未知工具执行
    assert out["pending_tool_calls"] == []
    # 消息里保留声明调用（merge 要按 call_id 回填 ToolMessage）
    ai = [m for m in out["messages"] if getattr(m, "tool_calls", None)]
    assert ai and ai[0].tool_calls[0]["name"] == DISPATCH_TOOL_NAME


def test_agent_node_defers_other_tools_in_declaration_round():
    bus = EventBus(queue.Queue())
    calls = [_decl(), {"name": "read_file", "args": {"path": "x"}, "id": "c2", "type": "tool_call"}]
    out = _agent_node(_state(_service(calls), bus))

    assert len(out["pending_subtasks"]) == 1
    assert out["pending_tool_calls"] == []
    assert any(
        "其它工具调用未执行" in str(getattr(m, "content", ""))
        for m in out["messages"]
    )


def test_declaration_rejected_at_cap():
    bus = EventBus(queue.Queue())
    out = _agent_node(
        _state(_service([_decl()]), bus, dispatch_rounds=3)
    )
    assert out["pending_subtasks"] == []
    # 达上限：声明当作普通工具调用走 tools 节点（无副作用）并提示模型自己收尾
    assert [tc["name"] for tc in out["pending_tool_calls"]] == [DISPATCH_TOOL_NAME]
    assert any(
        "派发已达上限" in str(getattr(m, "content", "")) for m in out["messages"]
    )


# ---------------- dispatch 节点：清单种子与预算升格 ----------------


def test_dispatch_node_seeds_todos_and_escalates_task_mode():
    bus = EventBus(queue.Queue())
    state = {
        "bus": bus,
        "db": None,
        "runtime": {},
        "pending_subtasks": [
            {"task": "写页面", "mode": "execute", "call_id": "c1"}
        ],
        "todos": [],
        "task_mode": False,
    }
    out = _dispatch_node(state)
    assert out["task_mode"] is True
    events = []
    while not bus._queue.empty():
        events.append(bus._queue.get_nowait())
    assert any(ev["event"] == "tool_start" for ev in events)


def test_dispatch_node_research_keeps_budget():
    out = _dispatch_node(
        {
            "bus": EventBus(queue.Queue()),
            "db": None,
            "runtime": {},
            "pending_subtasks": [
                {"task": "查资料", "mode": "research", "call_id": "c1"}
            ],
            "todos": [],
            "task_mode": False,
        }
    )
    assert out["task_mode"] is False


# ---------------- 子代理上下文包 ----------------


def test_context_pack_lists_workspace_with_mtime(tmp_path):
    (tmp_path / "pelican_bicycle.html").write_text("<html/>", encoding="utf-8")
    service = SimpleNamespace(
        settings=SimpleNamespace(tool_workspace=str(tmp_path))
    )
    pack = _build_context_pack(
        service, {"project_dir": str(tmp_path)}, "research"
    )
    assert "pelican_bicycle.html" in pack
    assert "历史遗留" in pack
    # 只读模式不能把写工具列给模型看
    assert "write_file" not in pack
