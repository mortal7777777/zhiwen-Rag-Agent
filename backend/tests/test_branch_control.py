"""子代理分支控制（6.7）：运行级取消令牌 + 按 subtask 取消 + SSE 观测事件。"""

from __future__ import annotations

import threading
from queue import Queue
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.agent.branch_control import RunCancellation
from app.agent.nodes import subagent as subagent_module
from app.agent.nodes.subagent import _subagent_node
from app.agent.state import EventBus
from app.api.agent import (
    router as agent_router,
    try_register_active_run,
    unregister_active_run,
)


class _Settings:
    tool_permission_mode = "ask"
    command_allowlist = ""
    agent_subagent_max_rounds = 2
    agent_subagent_exec_max_rounds = 3
    agent_subagent_max_llm_calls = 24


class _Rag:
    def acquire_llm(self):
        pass

    def release_llm(self):
        pass


class _FakeTool:
    def __init__(self, name: str, result: dict, on_invoke=None):
        self.name = name
        self.description = name
        self.result = result
        self.calls: list[dict] = []
        self._on_invoke = on_invoke

    def invoke(self, args):
        self.calls.append(args)
        if self._on_invoke:
            self._on_invoke()
        return dict(self.result)


class _SeqChat:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, config=None):
        self.calls += 1
        item = (
            self._responses.pop(0)
            if self._responses
            else AIMessage(content="（无更多响应）")
        )
        if isinstance(item, Exception):
            raise item
        return item


def _call(name: str = "read_file", args: dict | None = None, call_id: str = "t1"):
    return {
        "name": name,
        "args": args or {"path": "a.txt"},
        "id": call_id,
        "type": "tool_call",
    }


def _drain(bus: EventBus) -> list[dict]:
    events: list[dict] = []
    while not bus._queue.empty():
        events.append(bus._queue.get_nowait())
    return events


# ---------------- 取消令牌本身 ----------------


def test_cancel_branch_sets_only_that_branch():
    token = RunCancellation()
    first = token.register_branch("subtask_c1_0")
    second = token.register_branch("subtask_c1_1")

    assert token.cancel_branch("subtask_c1_0") is True
    assert first.is_set() is True
    assert second.is_set() is False
    assert token.is_set() is False

    # 幂等；未知分支返回 False
    assert token.cancel_branch("subtask_c1_0") is True
    assert token.cancel_branch("nope") is False
    assert token.branch_ids() == ["subtask_c1_0", "subtask_c1_1"]


def test_full_stop_cascades_and_late_registration_is_cancelled():
    token = RunCancellation()
    branch = token.register_branch("subtask_c1_0")
    token.set()
    assert token.is_set() is True
    assert branch.is_set() is True

    late = token.register_branch("subtask_c1_1")
    assert late.is_set() is True


def test_unregister_removes_branch():
    token = RunCancellation()
    event = token.register_branch("subtask_c1_0")
    token.unregister_branch("subtask_c1_0", event)
    assert token.branch_ids() == []
    assert token.cancel_branch("subtask_c1_0") is False


# ---------------- 子代理节点：观测事件 + 协作式取消 ----------------


def test_subagent_emits_observability_events(monkeypatch):
    monkeypatch.setattr(
        subagent_module, "_subagent_tools", lambda *a, **k: [tool]
    )
    tool = _FakeTool("read_file", {"summary": "ok"})
    chat = _SeqChat(
        [
            AIMessage(content="", tool_calls=[_call("read_file")]),
            AIMessage(content="结论：完成"),
        ]
    )
    bus = EventBus(Queue())
    deps = SimpleNamespace(
        service=SimpleNamespace(
            settings=_Settings(),
            chat=chat,
            chat_plain=_SeqChat([]),
            rag=_Rag(),
        ),
        bus=bus,
        db=None,
        stop_event=None,
    )
    state = {
        "runtime": {"conv_id": 1},
        "question": "原始问题",
        "sub_task": {
            "task": "子任务",
            "mode": "research",
            "call_id": "c1",
            "subtask_id": "subtask_c1_0",
        },
        "project_dir": None,
        "conversation_id": 1,
    }

    out = _subagent_node(state, SimpleNamespace(context=deps))

    events = _drain(bus)
    start = [e for e in events if e["event"] == "subagent_start"]
    progress = [e for e in events if e["event"] == "subagent_progress"]
    result = [e for e in events if e["event"] == "subagent_result"]
    assert len(start) == 1 and start[0]["data"]["subtask_id"] == "subtask_c1_0"
    assert start[0]["data"]["max_rounds"] == 2
    assert any(p["data"].get("phase") == "tool" and p["data"].get("tool") == "read_file" for p in progress)
    assert len(result) == 1
    assert result[0]["data"]["status"] == "done"
    assert result[0]["data"]["rounds"] == 2
    assert "tokens" in result[0]["data"]
    assert out["subagent_results"][0]["subtask_id"] == "subtask_c1_0"
    assert out["subagent_results"][0]["cancelled"] is False


def test_branch_cancel_stops_after_current_tool(monkeypatch):
    token = RunCancellation()

    def _cancel_mid_tool():
        # 取消发生在工具执行期间：本次工具结果保留，循环在下一个检查点退出
        assert token.cancel_branch("subtask_c1_0") is True

    tool = _FakeTool("read_file", {"summary": "ok"}, on_invoke=_cancel_mid_tool)
    monkeypatch.setattr(
        subagent_module, "_subagent_tools", lambda *a, **k: [tool]
    )
    chat = _SeqChat(
        [
            AIMessage(content="", tool_calls=[_call("read_file")]),
            AIMessage(content="不应该再调用模型"),
        ]
    )
    bus = EventBus(Queue())
    deps = SimpleNamespace(
        service=SimpleNamespace(
            settings=_Settings(),
            chat=chat,
            chat_plain=_SeqChat([]),
            rag=_Rag(),
        ),
        bus=bus,
        db=None,
        stop_event=token,
    )
    state = {
        "runtime": {"conv_id": 1},
        "question": "原始问题",
        "sub_task": {
            "task": "子任务",
            "mode": "research",
            "call_id": "c1",
            "subtask_id": "subtask_c1_0",
        },
        "project_dir": None,
        "conversation_id": 1,
    }

    out = _subagent_node(state, SimpleNamespace(context=deps))

    assert chat.calls == 1, "取消后不应再发起模型调用"
    assert tool.calls == [{"path": "a.txt"}]
    result = out["subagent_results"][0]
    assert result["cancelled"] is True
    assert "已取消" in result["summary"]
    assert token.branch_ids() == [], "分支结束后注册表应清理"

    events = _drain(bus)
    finished = [e for e in events if e["event"] == "subagent_result"]
    assert finished and finished[0]["data"]["status"] == "cancelled"


def test_full_stop_also_stops_branch(monkeypatch):
    token = RunCancellation()
    token.set()  # 整轮停止在分支启动前发生
    tool = _FakeTool("read_file", {"summary": "ok"})
    monkeypatch.setattr(
        subagent_module, "_subagent_tools", lambda *a, **k: [tool]
    )
    chat = _SeqChat([AIMessage(content="不应该被调用")])
    deps = SimpleNamespace(
        service=SimpleNamespace(
            settings=_Settings(),
            chat=chat,
            chat_plain=_SeqChat([]),
            rag=_Rag(),
        ),
        bus=EventBus(Queue()),
        db=None,
        stop_event=token,
    )
    state = {
        "runtime": {"conv_id": 1},
        "question": "q",
        "sub_task": {"task": "t", "mode": "research", "subtask_id": "subtask_c1_0"},
        "project_dir": None,
        "conversation_id": 1,
    }

    out = _subagent_node(state, SimpleNamespace(context=deps))

    assert chat.calls == 0
    assert out["subagent_results"][0]["cancelled"] is True


# ---------------- API：按 subtask 取消 ----------------


def test_cancel_subagent_endpoint_roundtrip():
    app = FastAPI()
    app.include_router(agent_router)
    client = TestClient(app)

    token = RunCancellation()
    run_id = try_register_active_run(token)
    assert run_id is not None
    branch = token.register_branch("subtask_c1_0")
    try:
        resp = client.post(f"/agent/cancel/{run_id}/subagent/subtask_c1_0")
        body = resp.json()
        assert resp.status_code == 200
        assert body["cancelled"] is True
        assert branch.is_set() is True

        missing = client.post(f"/agent/cancel/{run_id}/subagent/subtask_c1_9").json()
        assert missing["cancelled"] is False
        assert missing["reason"] == "subtask not found"

        unknown = client.post("/agent/cancel/nope/subagent/x").json()
        assert unknown["cancelled"] is False
        assert unknown["reason"] == "run not found"
    finally:
        unregister_active_run(run_id)


def test_cancel_subagent_endpoint_rejects_plain_event():
    """旧调用方只登记 threading.Event：分支取消不支持，明确返回原因。"""
    app = FastAPI()
    app.include_router(agent_router)
    client = TestClient(app)

    run_id = try_register_active_run(threading.Event())
    assert run_id is not None
    try:
        body = client.post(f"/agent/cancel/{run_id}/subagent/x").json()
        assert body["cancelled"] is False
        assert body["reason"] == "branch cancel unsupported"
    finally:
        unregister_active_run(run_id)
