"""ask_user_question：问题并入原生挂起，答案在恢复段合成 ToolMessage。

与 test_tools_hitl_interrupt.py 同一套假 interrupt 语义：第一段抛异常
（挂起），第二段返回 resume 值（恢复）。这里专门锁提问通道的载荷形状、
答案回填、缺失答案兜底与格式校验。
"""

from __future__ import annotations

import json
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
    def __init__(self, name):
        self.name = name
        self.calls: list[dict] = []

    def invoke(self, args):
        self.calls.append(dict(args))
        return {"summary": "已执行"}


def _settings():
    return SimpleNamespace(
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


def _make_env(calls):
    by_name: dict[str, _RecordingTool] = {}
    pending = []
    for name, call_id, args in calls:
        if name not in by_name:
            by_name[name] = _RecordingTool(name)
        pending.append({"name": name, "id": call_id, "args": args})
    bus = EventBus(queue.Queue())
    state = {
        "runtime": {},
        "messages": [],
        "pending_tool_calls": pending,
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
        service=SimpleNamespace(settings=_settings()),
        bus=bus,
        db=None,
        stop_event=None,
        tools=list(by_name.values()),
        title_holder=[],
        title_thread=None,
    )
    return state, SimpleNamespace(context=deps), list(by_name.values()), bus


def _events(bus):
    out = []
    while not bus._queue.empty():
        out.append(bus._queue.get_nowait())
    return out


def _fake_interrupt(monkeypatch, resume_value):
    seen: list[dict] = []

    def fake(payload):
        seen.append(payload)
        if len(seen) == 1:
            raise _FakeInterrupt(payload)
        return resume_value

    monkeypatch.setattr(tools_mod, "interrupt", fake)
    return seen


def _ask_call(call_id="q1"):
    return (
        "ask_user_question",
        call_id,
        {
            "questions": [
                {
                    "question": "选哪个方案？",
                    "header": "方案",
                    "options": [
                        {"label": "A", "description": "改动小"},
                        {"label": "B", "description": "更彻底"},
                    ],
                },
                {
                    "question": "是否包含测试？",
                    "multi_select": False,
                    "options": [{"label": "包含"}, {"label": "不包含"}],
                },
            ]
        },
    )


def test_ask_suspends_with_questions_and_synthesizes_answers(monkeypatch):
    state, runtime, tools, bus = _make_env([_ask_call()])
    seen = _fake_interrupt(
        monkeypatch,
        {
            "stopped": False,
            "decisions": {},
            "answers": {
                "q1": {
                    "items": [
                        {"index": 0, "selected": ["A"], "other": "", "skipped": False},
                        {
                            "index": 1,
                            "selected": [],
                            "other": "包含关键路径即可",
                            "skipped": False,
                        },
                    ]
                }
            },
        },
    )

    # 挂起段：不执行工具、不追加消息、不发事件
    with pytest.raises(_FakeInterrupt):
        _tools_node(state, runtime)
    assert tools[0].calls == []
    assert state["messages"] == []
    assert _events(bus) == []

    payload = seen[0]
    assert payload["questions"][0]["call_id"] == "q1"
    items = payload["questions"][0]["questions"]
    assert [q["question"] for q in items] == ["选哪个方案？", "是否包含测试？"]
    assert items[0]["header"] == "方案"
    assert [o["label"] for o in items[0]["options"]] == ["A", "B"]

    # 恢复段：答案合成 ToolMessage，工具本身不执行
    out = _tools_node(state, runtime)
    assert tools[0].calls == [], "提问工具不应真正执行"
    msgs = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert len(msgs) == 1 and msgs[0].tool_call_id == "q1"
    data = json.loads(msgs[0].content)
    assert data["summary"] == "用户已回答 2/2 个问题"
    assert data["answers"][0]["answer"] == "A"
    assert "包含关键路径即可" in data["answers"][1]["answer"]


def test_ask_missing_answers_defaults_to_skipped(monkeypatch):
    state, runtime, tools, bus = _make_env([_ask_call()])
    _fake_interrupt(monkeypatch, {"stopped": False, "decisions": {}})

    with pytest.raises(_FakeInterrupt):
        _tools_node(state, runtime)
    out = _tools_node(state, runtime)

    msgs = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    data = json.loads(msgs[0].content)
    assert data["summary"] == "用户已回答 0/2 个问题"
    assert all("跳过" in row["answer"] for row in data["answers"])


def test_ask_invalid_options_returns_error_without_suspending(monkeypatch):
    state, runtime, tools, bus = _make_env(
        [
            (
                "ask_user_question",
                "q1",
                {"questions": [{"question": "只有选项没有? ", "options": [{"label": "唯一"}]}]},
            )
        ]
    )
    called = []
    monkeypatch.setattr(tools_mod, "interrupt", lambda payload: called.append(payload))

    out = _tools_node(state, runtime)

    assert called == [], "格式不合法不应挂起"
    assert tools[0].calls == []
    msgs = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert msgs and "2~4" in msgs[0].content


def test_ask_allowed_in_plan_mode(monkeypatch):
    state, runtime, tools, bus = _make_env([_ask_call()])
    state["plan_only"] = True
    seen = _fake_interrupt(
        monkeypatch,
        {"stopped": False, "decisions": {}, "answers": {"q1": {"items": []}}},
    )

    with pytest.raises(_FakeInterrupt):
        _tools_node(state, runtime)
    assert seen and seen[0]["questions"], "计划模式下提问不应被敏感操作拦截"

    out = _tools_node(state, runtime)
    msgs = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert msgs and "permission" not in json.loads(msgs[0].content)


def test_ask_caps_total_questions_at_four(monkeypatch):
    questions = [
        {
            "question": f"问题{i}",
            "options": [{"label": "A"}, {"label": "B"}],
        }
        for i in range(6)
    ]
    state, runtime, tools, bus = _make_env(
        [("ask_user_question", "q1", {"questions": questions})]
    )
    seen = _fake_interrupt(
        monkeypatch,
        {"stopped": False, "decisions": {}, "answers": {}},
    )

    with pytest.raises(_FakeInterrupt):
        _tools_node(state, runtime)
    assert len(seen[0]["questions"][0]["questions"]) == 4
