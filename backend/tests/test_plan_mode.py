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


def test_exit_plan_mode_without_steps_is_ignored():
    calls = [{"name": PLAN_EXIT_TOOL_NAME, "id": "c4", "args": {"steps": []}}]
    state = _state([make_exit_plan_mode_tool()], calls)
    out = _tools_node(state, runtime_from_state(state))

    assert "plan_approval" not in state["runtime"]
    assert out["forced_final"] is False


class _PlainChat:
    """返回纯文本（无工具调用）的假模型，用于触发"想收尾"的分支。"""

    def __init__(self, text: str = "好的，我总结一下。") -> None:
        self._text = text

    def bind_tools(self, tools):
        return self

    def stream(self, messages, config=None):
        from langchain_core.messages import AIMessageChunk

        yield AIMessageChunk(content=self._text)


class _FakeRag:
    def acquire_llm(self):
        pass

    def release_llm(self):
        pass


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


# ---------------- 计划提交硬约束（plan_submit_push，2026-10-10） ----------------
# OpenCode Go 网关实测：模型在计划模式里探索正常，但收尾时把计划写成正文、
# 从不调 exit_plan_mode（确认卡永不出现）。无工具调用收尾时推回要求提交。


def _submit_push_state(chat, *, plan_only=True, push_count=0) -> dict:
    state = _constraint_state([], chat)
    # 清掉计划执行阶段的残留，避免 plan_push（todos 硬约束）与本分支混淆
    state["plan_map"] = []
    state["plan_steps"] = []
    state["plan_only"] = plan_only
    state["plan_submit_push_count"] = push_count
    return state


def test_plan_submit_push_on_text_only_finish():
    from app.agent.nodes.agent import _agent_node

    chat = _PlainChat("好的，执行计划如下：\n1. 新建 HTML 页面\n2. 截图核验")
    state = _submit_push_state(chat)
    out = _agent_node(state, runtime_from_state(state))

    assert out["force_continue"] is True
    assert out["plan_submit_push_count"] == 1
    assert any(
        "exit_plan_mode" in str(getattr(m, "content", ""))
        for m in out["messages"]
    ), "推回提示必须明确要求调用 exit_plan_mode 提交"


def test_plan_submit_push_released_at_limit_and_outside_plan():
    from app.agent.nodes.agent import _agent_node

    # 到达上限（3 次）后放行收尾，不再推回
    chat = _PlainChat("计划如下……")
    state = _submit_push_state(chat, push_count=3)
    out = _agent_node(state, runtime_from_state(state))
    assert out["force_continue"] is False
    assert out["plan_submit_push_count"] == 3

    # 非计划模式：完全不受影响
    chat2 = _PlainChat("普通回答")
    state2 = _submit_push_state(chat2, plan_only=False)
    out2 = _agent_node(state2, runtime_from_state(state2))
    assert out2["force_continue"] is False
    assert out2["plan_submit_push_count"] == 0


def test_plan_submit_push_still_gives_chance_after_forced_final():
    """预算耗尽先触发强制收尾（forced_final）时，计划未提交仍要推回给提交机会。

    2026-10-10 OpenCode Go 实测路径：22 步探索烧光预算 → tools 节点置
    forced_final → 模型以正文汇报、从不调 exit_plan_mode。推回不看
    forced_final，且此时用"预算耗尽版"提示（不留探索出口）。
    """
    from app.agent.nodes.agent import _agent_node

    chat = _PlainChat("预算已用尽，进度汇报：我已完成调研，计划如下……")
    state = _submit_push_state(chat)
    state["forced_final"] = True
    out = _agent_node(state, runtime_from_state(state))

    assert out["force_continue"] is True
    assert out["plan_submit_push_count"] == 1
    assert any(
        "不要再调用探索工具" in str(getattr(m, "content", ""))
        for m in out["messages"]
    ), "forced_final 下的推回要使用预算耗尽版提示（只留提交一条路）"


class _OkTool:
    name = "noop"

    def invoke(self, args):
        return {"summary": "ok"}


def test_budget_exhausted_in_plan_mode_asks_submit_not_report():
    """预算耗尽且计划未提交：tools 节点注入"立即提交"提示而非"汇报进度"。

    2026-10-10 实测：旧提示（budget_exhausted_notice 的"请给出进度汇报"）
    与提交要求打架，模型继续探索/以正文汇报，从不调 exit_plan_mode。
    """
    calls = [{"name": "noop", "id": "c1", "args": {}}]
    state = _state(
        [_OkTool()], calls, tool_calls_used=12, plan_only=True
    )
    out = _tools_node(state, runtime_from_state(state))

    contents = [
        str(getattr(m, "content", ""))
        for m in out["messages"]
        if getattr(m, "type", "") == "system"
    ]
    assert any("立即调用 exit_plan_mode" in c for c in contents)
    assert not any("进度汇报" in c for c in contents), "计划未提交时不劝汇报"
    assert out["forced_final"] is True


# finalize 的「等待计划确认」状态随旧路径删除：计划确认走原生挂起，
# 不再以 awaiting_approval 收尾（覆盖在 tests/test_tools_hitl_interrupt.py）。
