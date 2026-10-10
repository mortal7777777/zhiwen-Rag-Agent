"""tools 节点的"interrupt 之前零副作用"不变式（P1-4）。

为什么重要：敏感操作走原生 interrupt 挂起，**恢复时该节点会从头整段重跑**
（interrupt() 在恢复段直接返回决定值）。因此 interrupt 之前必须是纯计算：
一旦在它之前执行了工具、发了事件或写了库，恢复时就会重复发生一次。

这个不变式过去只写在代码注释里。本测试把 interrupt 打成哨兵异常，
断言"直到 interrupt 被调用，什么都没发生"。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.nodes import tools as tools_node_module


class _Sentinel(Exception):
    """哨兵：interrupt 被调用即抛出，用于截断节点执行。"""


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def emit(self, event: str, data) -> None:
        self.events.append((event, data))


class _RecordingTool:
    """假敏感工具：被 invoke 即记录（本测试里不应发生）。"""

    name = "write_file"
    description = "写文件"
    args_schema = None

    def __init__(self, invoked: list) -> None:
        self._invoked = invoked

    def invoke(self, args):
        self._invoked.append(args)
        return {"summary": "已写入", "relative_path": args.get("path")}


def _make_state() -> dict:
    return {
        "runtime": {"conv_id": None},
        "messages": [],
        "pending_tool_calls": [
            {
                "id": "c1",
                "name": "write_file",
                "args": {"path": "a.txt", "content": "x"},
            }
        ],
        "plan_only": False,
        "tool_trace": [],
        "sources": [],
        "tool_calls_used": 0,
        "failure_count": 0,
        "forced_final": False,
        "last_call_warned": False,
        "todos": [],
        "plan_steps": [],
        "plan_map": [],
    }


def test_no_side_effects_before_interrupt(monkeypatch):
    from app import runtime_config

    # 保证审批模式是默认的 ask（否则不敏感操作不会挂起）
    monkeypatch.setattr(runtime_config, "_overrides", {})

    invoked: list = []
    bus = _RecordingBus()
    captured: dict = {}

    def _fake_interrupt(payload):
        captured["payload"] = payload
        raise _Sentinel()

    monkeypatch.setattr(tools_node_module, "interrupt", _fake_interrupt)

    deps = SimpleNamespace(
        service=SimpleNamespace(
            settings=SimpleNamespace(tool_permission_mode="ask")
        ),
        bus=bus,
        db=None,
        stop_event=None,
        tools=[_RecordingTool(invoked)],
    )
    state = _make_state()

    with pytest.raises(_Sentinel):
        tools_node_module._tools_node(state, SimpleNamespace(context=deps))

    # 核心断言：interrupt 之前不得执行工具、不得发事件、不得写 state
    assert invoked == [], "interrupt 之前执行了工具（恢复时会重复执行）"
    assert bus.events == [], f"interrupt 之前发了事件：{bus.events}"
    assert state["tool_trace"] == []
    assert state["sources"] == []

    # 挂起载荷必须是"待审批项"，供前端渲染审批卡
    payload = captured["payload"]
    assert payload["kind"] == "approval"
    assert [op["call_id"] for op in payload["ops"]] == ["c1"]
    assert payload["ops"][0]["summary"], "审批卡需要一句话描述"
