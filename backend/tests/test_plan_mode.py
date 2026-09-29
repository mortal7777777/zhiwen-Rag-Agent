"""计划模式：模型自主进入（只读探索）→ 结构化提交 → 用户确认后按同计划执行。

开关语义：前端 plan_mode_allowed=false 时后端不注册 enter/exit 工具，
"关掉就不会用"是硬否决；开启时由模型判断值不值得先出计划。
"""

from __future__ import annotations

import json
import queue
from types import SimpleNamespace

from app.agent.nodes.tools import _tools_node
from app.agent.state import EventBus
from app.agent.tools import (
    PLAN_ENTER_TOOL_NAME,
    PLAN_EXIT_TOOL_NAME,
    make_enter_plan_mode_tool,
    make_exit_plan_mode_tool,
)
from _node_rt import runtime_from_state  # noqa: E402


class _FakeTool:
    """名字对了就能被识别为敏感工具（拦截分支不会真的执行它）。"""

    def __init__(self, name):
        self.name = name

    def invoke(self, args):
        raise AssertionError("计划模式下敏感工具不应被执行")


def _settings():
    return SimpleNamespace(
        reasoning_summary_enabled=False,
        verify_auto_detect=False,
        verify_command="",
        tool_permission_mode="allow",
        permission_timeout=0,
        command_allowlist="",
        checkpoint_enabled=False,
        agent_max_iterations=12,
        agent_max_failures=3,
        task_mode_detect=True,
        agent_task_max_failures=6,
        agent_task_max_iterations=50,
    )


def _state(tools, calls, **over):
    state = {
        "service": SimpleNamespace(settings=_settings()),
        "bus": EventBus(queue.Queue()),
        "db": None,
        "runtime": {},
        "messages": [],
        "tools": tools,
        "pending_tool_calls": calls,
        "tool_trace": [],
        "sources": [],
        "tool_calls_used": 1,
        "forced_final": False,
        "last_call_warned": True,
        "todos": [],
        "plan_map": [],
        "plan_steps": [],
        "plan_done_count": 0,
        "plan_push_count": 0,
        "stop_event": None,
        "counter": [0],
    }
    state.update(over)
    return state


def _events(bus):
    out = []
    while not bus._queue.empty():
        out.append(bus._queue.get_nowait())
    return out


# ---------------- 工具本身 ----------------


def test_plan_tools_are_defined():
    assert make_enter_plan_mode_tool().name == PLAN_ENTER_TOOL_NAME
    exit_tool = make_exit_plan_mode_tool()
    assert exit_tool.name == PLAN_EXIT_TOOL_NAME
    # 结构化参数：steps 必须是一步步的清单（前端据此渲染确认卡）
    assert "steps" in exit_tool.args_schema.model_fields


# ---------------- 进入计划模式 ----------------


def test_enter_plan_mode_sets_flag_and_injects_rules():
    calls = [{"name": PLAN_ENTER_TOOL_NAME, "id": "c1", "args": {"reason": "改动大"}}]
    state = _state([make_enter_plan_mode_tool()], calls)
    out = _tools_node(state, runtime_from_state(state))

    assert out["plan_only"] is True
    assert any(
        "计划模式（只读）" in str(getattr(m, "content", ""))
        for m in out["messages"]
    )
    assert not out["forced_final"], "进入计划模式不应立刻收尾"


def test_plan_mode_hint_never_splits_tool_use_from_result():
    """计划模式提示必须排在所有 ToolMessage 之后。

    Anthropic 要求 assistant 的 tool_use 紧邻其 tool_result；中间夹一条
    SystemMessage（会被 normalizer 转成 HumanMessage）就是 400：
    实测 `messages.2: tool_use ids were found without tool_result blocks`。
    同轮同时调 enter_plan_mode 与普通工具时最容易踩到。
    """
    calls = [
        {"name": PLAN_ENTER_TOOL_NAME, "id": "c1", "args": {"reason": "先对齐"}},
        {"name": "list_dir", "id": "c2", "args": {"path": "."}},
    ]
    state = _state(
        [make_enter_plan_mode_tool(), _ListDirStub()], calls
    )
    out = _tools_node(state, runtime_from_state(state))

    seen_system = False
    for m in out["messages"]:
        if getattr(m, "type", "") == "system":
            seen_system = True
        elif getattr(m, "type", "") == "tool" and seen_system:
            raise AssertionError("ToolMessage 出现在 SystemMessage 之后（会破坏 tool_use 配对）")
    assert state["plan_only"] is True


class _ListDirStub:
    name = "list_dir"

    def invoke(self, args):
        return {"summary": "列出 3 项", "path": args.get("path")}


def test_plan_mode_blocks_sensitive_tools():
    """进入计划模式后，写文件被拦截且提示改用 exit_plan_mode 提交计划。"""
    calls = [{"name": "write_file", "id": "c2", "args": {"path": "x.html", "content": "hi"}}]
    state = _state([_FakeTool("write_file")], calls, plan_only=True)
    out = _tools_node(state, runtime_from_state(state))

    tool_rows = [
        json.loads(m.content)
        for m in out["messages"]
        if getattr(m, "type", "") == "tool"
    ]
    assert tool_rows and "exit_plan_mode" in json.dumps(tool_rows, ensure_ascii=False)


# ---------------- 提交计划 ----------------


def test_exit_plan_mode_submits_structured_plan():
    calls = [
        {
            "name": PLAN_EXIT_TOOL_NAME,
            "id": "c3",
            "args": {"steps": ["写 pelican.html", "跑验证命令", "修正问题"], "summary": "做一个页面"},
        }
    ]
    state = _state([make_exit_plan_mode_tool()], calls)
    out = _tools_node(state, runtime_from_state(state))

    approval = state["runtime"]["plan_approval"]
    assert approval["steps"] == ["写 pelican.html", "跑验证命令", "修正问题"]
    assert state["plan_steps"] == approval["steps"]
    # 提交计划后本轮收尾（不再派工具），等用户确认
    assert out["forced_final"] is True
    events = _events(state["bus"])
    assert any(ev["event"] == "plan_approval" for ev in events)


def test_exit_plan_mode_without_steps_is_ignored():
    calls = [{"name": PLAN_EXIT_TOOL_NAME, "id": "c4", "args": {"steps": []}}]
    state = _state([make_exit_plan_mode_tool()], calls)
    out = _tools_node(state, runtime_from_state(state))

    assert "plan_approval" not in state["runtime"]
    assert out["forced_final"] is False


# ---------------- 提交后立即收尾（不再多跑一轮模型） ----------------


class _NoCallChat:
    def __init__(self):
        self.called = False

    def bind_tools(self, tools):
        return self

    def stream(self, messages, config=None):
        self.called = True
        raise AssertionError("计划提交后不应再调用模型")


class _FakeRag:
    def acquire_llm(self):
        pass

    def release_llm(self):
        pass


def test_agent_node_short_circuits_after_plan_approval():
    """实测提交计划后模型还会再跑两轮共 4 分钟，这里直接渲染计划收尾。"""
    import time

    from app.agent.nodes.agent import _agent_node

    chat = _NoCallChat()
    bus = EventBus(queue.Queue())
    state = {
        "service": SimpleNamespace(
            chat=chat,
            chat_plain=chat,
            rag=_FakeRag(),
            settings=SimpleNamespace(
                agent_max_iterations=12,
                agent_llm_stall_timeout_s=5.0,
                agent_max_dispatch_rounds=3,
            ),
        ),
        "bus": bus,
        "runtime": {
            "started": time.perf_counter(),
            "final_text": "",
            "plan_approval": {"steps": ["第一步", "第二步"], "summary": "目标"},
        },
        "messages": [],
        "tools": [],
        "tool_calls_used": 3,
        "forced_final": True,
        "last_call_warned": False,
        "todos": [],
        "plan_map": [],
        "plan_steps": ["第一步", "第二步"],
        "plan_done_count": 0,
        "plan_push_count": 0,
        "pending_tool_calls": [],
        "stop_event": None,
        "question": "写个页面",
    }

    out = _agent_node(state, runtime_from_state(state))

    assert chat.called is False, "计划提交后不应再发起 LLM 调用"
    assert "第一步" in state["runtime"]["final_text"]
    assert out["pending_tool_calls"] == []
    assert any(ev["event"] == "token" for ev in _events(bus))


class _PlainChat:
    """返回纯文本（无工具调用）的假模型，用于触发"想收尾"的分支。"""

    def __init__(self, text: str = "好的，我总结一下。") -> None:
        self._text = text

    def bind_tools(self, tools):
        return self

    def stream(self, messages, config=None):
        from langchain_core.messages import AIMessageChunk

        yield AIMessageChunk(content=self._text)


def _constraint_state(todos: list[dict], chat) -> dict:
    import time

    return {
        "service": SimpleNamespace(
            chat=chat,
            chat_plain=chat,
            rag=_FakeRag(),
            settings=SimpleNamespace(
                agent_max_iterations=20,
                agent_llm_stall_timeout_s=5.0,
                agent_max_dispatch_rounds=3,
            ),
        ),
        "bus": EventBus(queue.Queue()),
        "runtime": {"started": time.perf_counter(), "final_text": ""},
        "messages": [],
        "tools": [object()],
        "tool_calls_used": 2,
        "forced_final": False,
        "last_call_warned": False,
        "todos": todos,
        "plan_map": [{"step": "整理总结", "tool_hint": ""}],
        "plan_steps": ["整理总结"],
        "plan_done_count": 0,
        "plan_push_count": 0,
        "pending_tool_calls": [],
        "stop_event": None,
        "question": "整理一下",
    }


def test_hard_constraint_triggers_for_hintless_step():
    """未完成步骤不再按关键词 hint 过滤——纯推理步骤（hint 为空）也会触发硬约束。

    旧实现要求剩余步骤里有关键词 hint 才推模型继续，而 hint 是猜的：漏判会让
    约束整条失效。现在只看"还有未完成步骤"这个事实，模型要么继续，要么按提示
    用 todo_update complete/remove 显式收尾。
    """
    from app.agent.nodes.agent import _agent_node

    state = _constraint_state(
        [{"text": "整理总结", "done": False, "source": "plan"}], _PlainChat()
    )
    out = _agent_node(state, runtime_from_state(state))

    assert out["force_continue"] is True
    assert out["plan_push_count"] == 1
    assert any(
        "【计划硬约束】" in str(getattr(m, "content", "")) for m in out["messages"]
    ), "推送语要出现，模型才知道清单还没清完"


def test_hard_constraint_releases_when_all_steps_done():
    from app.agent.nodes.agent import _agent_node

    state = _constraint_state(
        [{"text": "整理总结", "done": True, "source": "plan"}], _PlainChat()
    )
    out = _agent_node(state, runtime_from_state(state))

    assert out["force_continue"] is False
    assert out["plan_push_count"] == 0
    assert not any(
        "【计划硬约束】" in str(getattr(m, "content", "")) for m in out["messages"]
    )


# ---------------- finalize：等待确认状态 ----------------


def test_finalize_marks_awaiting_approval():
    from app.agent.nodes.finalize import _finalize_node

    bus = EventBus(queue.Queue())
    state = {
        "service": SimpleNamespace(
            settings=SimpleNamespace(
                checkpoint_enabled=False, trajectory_compress_enabled=False
            )
        ),
        "bus": bus,
        "db": None,
        "runtime": {
            "final_text": "计划如下…",
            "status": "ok",
            "plan_approval": {"steps": ["第一步"], "summary": ""},
        },
        "sources": [],
        "tool_trace": [],
        "question": "写个页面",
        "messages": [],
        "plan_steps": ["第一步"],
        "todos": [],
    }
    _finalize_node(state, runtime_from_state(state))

    assert state["runtime"]["status"] == "ok", "提交计划不是错误"
    events = _events(bus)
    assert any(ev["event"] == "plan" for ev in events)
