"""原生 interrupt 审批：挂起前纯计算、恢复段副作用只发生一次。

真实语义：节点在 interrupt() 处抛异常挂起（本段未提交），恢复时节点
从头重跑、interrupt() 返回决定。这里用假 interrupt 模拟这两段调用，
断言"执行/消息追加/事件"只在第二段发生一次。
"""

from __future__ import annotations

import queue
from types import SimpleNamespace

import pytest

from app.agent.nodes import tools as tools_mod
from app.agent.nodes.tools import _tools_node
from app.agent.state import EventBus
from langchain_core.messages import ToolMessage


class _FakeInterrupt(Exception):
    def __init__(self, payload):
        super().__init__("interrupted")
        self.payload = payload


class _RecordingTool:
    """每次 invoke 记录一次，用于断言恢复后只执行一次。"""

    def __init__(self, name):
        self.name = name
        self.calls: list[dict] = []

    def invoke(self, args):
        self.calls.append(dict(args))
        return {"summary": "已执行", "path": args.get("path", "")}


def _settings(**overrides):
    base = dict(
        reasoning_summary_enabled=False,
        verify_auto_detect=False,
        verify_command="",
        tool_permission_mode="ask",
        permission_timeout=0,
        command_allowlist="",
        agent_max_iterations=12,
        agent_max_failures=3,
        task_mode_detect=True,
        agent_task_max_failures=6,
        agent_task_max_iterations=50,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_env(tool_names_and_calls, settings=None):
    by_name: dict[str, _RecordingTool] = {}
    calls = []
    for name, call_id, args in tool_names_and_calls:
        # 同名工具共用一个实例（真实注册表里 tools_by_name 也是按名字取）
        if name not in by_name:
            by_name[name] = _RecordingTool(name)
        calls.append({"name": name, "id": call_id, "args": args})
    tools = list(by_name.values())
    bus = EventBus(queue.Queue())
    state = {
        "runtime": {},
        "messages": [],
        "pending_tool_calls": calls,
        "tool_trace": [],
        "sources": [],
        "tool_calls_used": 0,
        "forced_final": False,
        "last_call_warned": False,
        "todos": [],
        "plan_map": [],
        "plan_steps": [],
        "plan_done_count": 0,
        "plan_push_count": 0,
    }
    deps = SimpleNamespace(
        service=SimpleNamespace(settings=settings or _settings()),
        bus=bus,
        db=None,
        stop_event=None,
        tools=tools,
        title_holder=[],
        title_thread=None,
    )
    return state, SimpleNamespace(context=deps), tools, bus


def _events(bus):
    out = []
    while not bus._queue.empty():
        out.append(bus._queue.get_nowait())
    return out


def _install_fake_interrupt(monkeypatch, decisions):
    """首次调用抛出（挂起），之后返回决定（恢复段）。"""
    seen: list[dict] = []

    def fake(payload):
        seen.append(payload)
        if len(seen) == 1:
            raise _FakeInterrupt(payload)
        return {"stopped": False, "decisions": decisions}

    monkeypatch.setattr(tools_mod, "interrupt", fake)
    return seen


def test_suspend_is_side_effect_free_and_resume_executes_once(monkeypatch):
    state, runtime, tools, bus = _make_env(
        [("delete_file", "call_1", {"path": "a.txt"})]
    )
    seen = _install_fake_interrupt(monkeypatch, {"call_1": {"approve": True}})

    # 第一段：挂起（interrupt 抛出，节点未提交任何状态）
    with pytest.raises(_FakeInterrupt):
        _tools_node(state, runtime)

    assert tools[0].calls == [], "挂起段不得执行工具"
    assert state["messages"] == [], "挂起段不得追加消息"
    assert _events(bus) == [], "挂起段不得发任何事件"
    assert state["tool_trace"] == []

    payload = seen[0]
    assert payload["kind"] == "approval"
    assert "created_at" in payload  # watchdog 的批次戳
    assert [op["call_id"] for op in payload["ops"]] == ["call_1"]

    # 第二段：恢复（节点从头重跑，interrupt 返回决定）
    out = _tools_node(state, runtime)

    assert len(tools[0].calls) == 1, "恢复后工具只执行一次"
    tool_messages = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1, "ToolMessage 只追加一次"
    assert tool_messages[0].tool_call_id == "call_1"
    names = [e["event"] for e in _events(bus)]
    assert names.count("permission_resolved") == 1
    assert names.count("tool_start") == 1
    assert names.count("tool_result") == 1
    assert "permission_request" not in names  # 新路径不再逐条请求
    assert out["tool_calls_used"] == 1


def test_resume_deny_skips_execution(monkeypatch):
    state, runtime, tools, bus = _make_env(
        [("delete_file", "call_1", {"path": "a.txt"})]
    )
    _install_fake_interrupt(
        monkeypatch,
        {"call_1": {"approve": False, "reason": "目标文件很重要"}},
    )
    with pytest.raises(_FakeInterrupt):
        _tools_node(state, runtime)
    out = _tools_node(state, runtime)

    assert tools[0].calls == []
    joined = "".join(
        str(m.content) for m in out["messages"] if isinstance(m, ToolMessage)
    )
    assert "目标文件很重要" in joined
    resolved = [e for e in _events(bus) if e["event"] == "permission_resolved"]
    assert resolved and resolved[0]["data"]["approved"] is False


def test_missing_decision_defaults_to_deny(monkeypatch):
    state, runtime, tools, bus = _make_env(
        [("delete_file", "call_1", {"path": "a.txt"})]
    )
    _install_fake_interrupt(monkeypatch, {})  # 决定缺失
    with pytest.raises(_FakeInterrupt):
        _tools_node(state, runtime)
    out = _tools_node(state, runtime)

    assert tools[0].calls == [], "缺决定按拒绝处理，不得执行"
    assert out["messages"], "仍需回填 ToolMessage 说明被拒"


def test_same_command_in_one_round_asks_once(monkeypatch):
    state, runtime, tools, bus = _make_env(
        [
            ("bash", "call_1", {"command": "rm a.txt"}),
            ("bash", "call_2", {"command": "rm a.txt"}),
        ]
    )
    seen = _install_fake_interrupt(monkeypatch, {"call_1": {"approve": True}})
    with pytest.raises(_FakeInterrupt):
        _tools_node(state, runtime)
    out = _tools_node(state, runtime)

    assert len(seen[0]["ops"]) == 1, "同轮同命令只出一张审批项"
    assert seen[0]["ops"][0]["also_covers"] == ["call_2"]
    # 两个调用都放行、都执行（去重只影响审批，不影响执行）
    assert len(tools[0].calls) == 2
    assert len([m for m in out["messages"] if isinstance(m, ToolMessage)]) == 2


def test_plan_suspension_carries_plan_and_approval_continues(monkeypatch):
    """计划提交并入同一次挂起：确认（可编辑）后同一 run 继续，不再 forced_final。"""
    state, runtime, tools, bus = _make_env(
        [
            (
                "exit_plan_mode",
                "p1",
                {"steps": ["先看代码", "再写文件"], "summary": "两步行事"},
            )
        ]
    )
    state["plan_only"] = True
    seen = _install_fake_interrupt(
        monkeypatch,
        {},
    )

    def fake(payload):
        seen.append(payload)
        if len(seen) == 1:
            raise _FakeInterrupt(payload)
        return {
            "stopped": False,
            "decisions": {},
            "plan": {"approved": True, "steps": ["先看代码", "再写文件（改过）"], "reason": ""},
        }

    monkeypatch.setattr(tools_mod, "interrupt", fake)

    with pytest.raises(_FakeInterrupt):
        _tools_node(state, runtime)

    payload = seen[0]
    assert payload["ops"] == [], "计划模式无敏感操作"
    assert payload["plan"]["call_id"] == "p1"
    assert payload["plan"]["steps"] == ["先看代码", "再写文件"]
    assert tools[0].calls == [], "exit_plan_mode 工具不真正执行"

    out = _tools_node(state, runtime)

    assert out["plan_steps"] == ["先看代码", "再写文件（改过）"], "用编辑后的步骤"
    assert out["plan_only"] is False, "确认后退出计划模式"
    assert out["forced_final"] is False, "不再强制收尾，同一 run 继续"
    joined = "".join(str(m.content) for m in out["messages"])
    assert "计划已确认" in joined
    assert "用户已确认执行计划" in joined
    names = [e["event"] for e in _events(bus)]
    assert "plan" in names
    assert "plan_approval" not in names, "interrupt 路径不再走旧的 plan_approval 收尾"


def test_high_risk_command_forced_to_confirm_even_in_allow_mode(monkeypatch):
    """allow 模式下普通敏感操作自动批准，但高危命令必须挂起确认。"""
    state, runtime, tools, bus = _make_env(
        [("bash", "call_1", {"command": "rm -rf /"})]
    )
    state["runtime"]["conv_id"] = 991
    runtime.context.service.settings.tool_permission_mode = "allow"
    seen = _install_fake_interrupt(
        monkeypatch, {"call_1": {"approve": True, "remember_forever": True}}
    )

    with pytest.raises(_FakeInterrupt):
        _tools_node(state, runtime)
    assert tools[0].calls == [], "高危命令挂起段不得执行"
    op = seen[0]["ops"][0]
    assert op["risk"] == "high"
    assert op["risk_reason"]
    assert op["rememberable"] is False

    out = _tools_node(state, runtime)
    assert len(tools[0].calls) == 1, "批准后只执行一次"

    # 伪造的 remember 字段被忽略：高危命令不进会话白名单
    from app.permissions import get_permission_manager

    assert (
        get_permission_manager().is_session_allowed(991, "rm -rf /") is False
    )
    resolved = [e for e in _events(bus) if e["event"] == "permission_resolved"]
    assert resolved and resolved[0]["data"]["risk"] == "high"


def test_allow_mode_benign_write_still_auto_approved():
    """allow 模式未被高危底线误伤：普通写文件不弹确认。"""
    state, runtime, tools, bus = _make_env(
        [("write_file", "call_1", {"path": "a.txt", "content": "hi"})]
    )
    runtime.context.service.settings.tool_permission_mode = "allow"

    out = _tools_node(state, runtime)

    assert len(tools[0].calls) == 1
    assert not [
        e for e in _events(bus) if e["event"] == "permission_resolved"
    ]
    assert out["tool_calls_used"] == 1


def test_allow_mode_high_risk_file_write_forced_to_confirm(monkeypatch):
    """allow 模式下写 user_tools 或 .env 等高危路径仍必须挂起确认。"""
    state, runtime, tools, bus = _make_env(
        [
            (
                "write_file",
                "call_1",
                {"path": "backend/app/user_tools/backdoor.py", "content": "x"},
            )
        ]
    )
    runtime.context.service.settings.tool_permission_mode = "allow"
    seen = _install_fake_interrupt(monkeypatch, {"call_1": {"approve": False}})

    with pytest.raises(_FakeInterrupt):
        _tools_node(state, runtime)
    assert tools[0].calls == [], "高危文件写入在 allow 模式下也必须先挂起确认"
    assert seen[0]["ops"][0]["risk"] == "high"



def test_plan_suspension_rejection_stays_in_plan_mode(monkeypatch):
    state, runtime, tools, bus = _make_env(
        [("exit_plan_mode", "p1", {"steps": ["只有一步"], "summary": ""})]
    )
    state["plan_only"] = True
    seen = []

    def fake(payload):
        seen.append(payload)
        if len(seen) == 1:
            raise _FakeInterrupt(payload)
        return {
            "stopped": False,
            "decisions": {},
            "plan": {"approved": False, "reason": "太笼统，先补充验收标准"},
        }

    monkeypatch.setattr(tools_mod, "interrupt", fake)
    with pytest.raises(_FakeInterrupt):
        _tools_node(state, runtime)
    out = _tools_node(state, runtime)

    assert out["plan_only"] is True, "拒绝后仍在计划模式，可调整后重新提交"
    assert out["forced_final"] is False
    joined = "".join(str(m.content) for m in out["messages"])
    assert "用户未确认该计划" in joined and "太笼统" in joined




def test_verify_env_error_not_counted_as_file_failure(monkeypatch):
    """解释器缺失是环境问题：不增加 verify_fail、不诱导模型重写文件。"""
    settings = _settings()
    settings.tool_permission_mode = "allow"
    state, runtime, tools, bus = _make_env(
        [("write_file", "w1", {"path": "a.txt", "content": "x"})],
        settings=settings,
    )
    monkeypatch.setattr(
        tools_mod,
        "_run_verify",
        lambda *a, **k: [
            {
                "path": "a.txt",
                "command": "py -m py_compile a.txt",
                "exit_code": -1,
                "error": "验证环境不可用：No such file or directory",
                "output": "missing",
                "environment_error": True,
            }
        ],
    )
    out = _tools_node(state, runtime)
    assert out.get("verify_env_warned") is True
    assert int(out.get("verify_fail_count") or 0) == 0
    joined = "".join(str(m.content) for m in out["messages"])
    assert "写后验证不可用" in joined


def test_mutation_clears_per_run_query_cache():
    """同轮 write 之后 read 必须重新执行：旧缓存不能冒充写后状态。"""
    settings = _settings()
    settings.tool_permission_mode = "allow"
    state, runtime, tools, bus = _make_env(
        [
            ("read_file", "r1", {"path": "a.txt"}),
            ("write_file", "w0", {"path": "a.txt", "content": "old"}),
        ],
        settings=settings,
    )
    by_name = {t.name: t for t in tools}

    def run(name, call_id, args):
        state["pending_tool_calls"] = [{"name": name, "id": call_id, "args": args}]
        state["messages"] = []
        return _tools_node(state, runtime)

    run("read_file", "r1", {"path": "a.txt"})
    assert len(by_name["read_file"].calls) == 1
    run("read_file", "r2", {"path": "a.txt"})
    assert len(by_name["read_file"].calls) == 1, "同参重复查询应命中缓存"

    run("write_file", "w1", {"path": "a.txt", "content": "new"})
    assert len(by_name["write_file"].calls) == 1

    run("read_file", "r3", {"path": "a.txt"})
    assert len(by_name["read_file"].calls) == 2, "写操作后缓存必须失效"


# ---------------- acceptEdits 中间档 ----------------


def test_acceptedits_auto_approves_workspace_file_edits():
    """acceptEdits：工作区内的写/改/删自动批准，不挂起。"""
    state, runtime, tools, bus = _make_env(
        [
            ("write_file", "call_1", {"path": "a.txt", "content": "x"}),
            (
                "edit_file",
                "call_2",
                {"path": "a.txt", "old_string": "x", "new_string": "y"},
            ),
            ("delete_file", "call_3", {"path": "a.txt"}),
        ],
        settings=_settings(tool_permission_mode="acceptedits"),
    )
    out = _tools_node(state, runtime)

    assert [len(t.calls) for t in tools] == [1, 1, 1], "文件写/改/删不应弹审批"
    assert len([m for m in out["messages"] if isinstance(m, ToolMessage)]) == 3


def test_acceptedits_still_asks_for_commands(monkeypatch):
    """命令工具不在 acceptEdits 覆盖范围，仍需人工确认。"""
    state, runtime, tools, bus = _make_env(
        [("bash", "call_1", {"command": "mkdir out"})],
        settings=_settings(tool_permission_mode="acceptedits"),
    )
    seen = _install_fake_interrupt(monkeypatch, {"call_1": {"approve": True}})

    with pytest.raises(_FakeInterrupt):
        _tools_node(state, runtime)

    assert [op["call_id"] for op in seen[0]["ops"]] == ["call_1"]


def test_acceptedits_high_risk_file_still_asks(monkeypatch):
    """高危文件路径（.env 等）不受 acceptEdits 影响，强制确认。"""
    state, runtime, tools, bus = _make_env(
        [("write_file", "call_1", {"path": ".env", "content": "A=1"})],
        settings=_settings(tool_permission_mode="acceptedits"),
    )
    seen = _install_fake_interrupt(monkeypatch, {"call_1": {"approve": True}})

    with pytest.raises(_FakeInterrupt):
        _tools_node(state, runtime)

    op = seen[0]["ops"][0]
    assert op["risk"] == "high"
    assert "密钥" in op["risk_reason"]


# ---------------- Docker 沙箱内命令免确认 ----------------


def test_docker_sandbox_auto_allows_commands(monkeypatch):
    """command_sandbox=docker + sandbox_auto_allow：普通命令免确认。"""
    state, runtime, tools, bus = _make_env(
        [("bash", "call_1", {"command": "mkdir out"})],
        settings=_settings(sandbox_auto_allow=True),
    )
    state["runtime"]["command_sandbox"] = "docker"
    seen = _install_fake_interrupt(monkeypatch, {})

    out = _tools_node(state, runtime)

    assert seen == [], "Docker 沙箱内普通命令不应挂起"
    assert len(tools[0].calls) == 1
    assert len([m for m in out["messages"] if isinstance(m, ToolMessage)]) == 1


def test_sandbox_auto_allow_requires_docker(monkeypatch):
    """非 Docker 执行环境即使开了免确认，命令仍要人工确认。"""
    state, runtime, tools, bus = _make_env(
        [("bash", "call_1", {"command": "mkdir out"})],
        settings=_settings(sandbox_auto_allow=True),
    )
    state["runtime"]["command_sandbox"] = "subprocess"
    seen = _install_fake_interrupt(monkeypatch, {"call_1": {"approve": True}})

    with pytest.raises(_FakeInterrupt):
        _tools_node(state, runtime)

    assert [op["call_id"] for op in seen[0]["ops"]] == ["call_1"]


def test_sandbox_auto_allow_keeps_high_risk_asking(monkeypatch):
    """沙箱免确认不放行高危命令：高危底线始终优先。"""
    state, runtime, tools, bus = _make_env(
        [("bash", "call_1", {"command": "rm -rf /"})],
        settings=_settings(sandbox_auto_allow=True),
    )
    state["runtime"]["command_sandbox"] = "docker"
    seen = _install_fake_interrupt(monkeypatch, {"call_1": {"approve": True}})

    with pytest.raises(_FakeInterrupt):
        _tools_node(state, runtime)

    assert seen[0]["ops"][0]["risk"] == "high"
