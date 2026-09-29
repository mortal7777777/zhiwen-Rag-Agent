"""resume 端点与挂起闸门：409 校验、决定透传、停止语义（无真实网络）。"""

from __future__ import annotations

import json
import threading

import anyio
import pytest
from fastapi import HTTPException

from app.api.agent import (
    agent_resume,
    agent_stream,
    register_active_run,
    unregister_active_run,
)
from app.schemas import (
    AgentApprovalDecision,
    AgentChatRequest,
    AgentPlanDecision,
    AgentResumeRequest,
)


PENDING_PAYLOAD = {
    "kind": "approval",
    "created_at": 1.0,
    "ops": [
        {
            "call_id": "c1",
            "also_covers": [],
            "name": "delete_file",
            "arguments": {"path": "a.txt"},
            "summary": "删除文件 a.txt",
        }
    ],
}


class ResumeService:
    def __init__(self, pending: bool = True, payload: dict | None = None):
        self.pending = pending
        self.payload = payload or PENDING_PAYLOAD
        self.got: dict | None = None

    def has_pending_approval(self, conversation_id):
        return self.pending

    def pending_approval_payload(self, conversation_id):
        return self.payload if self.pending else None

    def resume(
        self,
        conversation_id,
        decisions,
        stopped=False,
        plan=None,
        db=None,
        stop_event=None,
    ):
        self.got = {
            "conversation_id": conversation_id,
            "decisions": decisions,
            "stopped": stopped,
            "plan": plan,
        }
        yield {"event": "session", "data": {"conversation_id": conversation_id}}
        yield {"event": "done", "data": {"ok": True}}

    def run(self, **kwargs):
        yield {"event": "session", "data": {}}
        yield {"event": "done", "data": {}}


async def _drain(response) -> list[dict]:
    frames = []
    async for frame in response.body_iterator:
        text = frame if isinstance(frame, str) else frame.decode("utf-8")
        if text.startswith("data: "):
            frames.append(json.loads(text.strip()[6:]))
    return frames


def test_resume_without_pending_raises_409():
    with pytest.raises(HTTPException) as exc:
        anyio.run(
            agent_resume,
            AgentResumeRequest(conversation_id=1),
            ResumeService(pending=False),
        )
    assert exc.value.status_code == 409


def test_resume_passes_decisions_and_streams_done():
    service = ResumeService()

    async def scenario():
        response = await agent_resume(
            AgentResumeRequest(
                conversation_id=5,
                decisions=[
                    AgentApprovalDecision(
                        call_id="c1", approve=False, reason="不允许"
                    )
                ],
            ),
            service,
        )
        return await _drain(response)

    frames = anyio.run(scenario)
    assert service.got["conversation_id"] == 5
    assert service.got["decisions"]["c1"]["approve"] is False
    assert service.got["decisions"]["c1"]["reason"] == "不允许"
    assert service.got["stopped"] is False
    assert any(f["event"] == "done" for f in frames)
    # session 帧带上 run_id（客户端可取消）
    session = next(f for f in frames if f["event"] == "session")
    assert session["data"].get("run_id")


def test_resume_stop_fills_denies():
    service = ResumeService()

    async def scenario():
        response = await agent_resume(
            AgentResumeRequest(conversation_id=5, stopped=True),
            service,
        )
        return await _drain(response)

    anyio.run(scenario)
    assert service.got["stopped"] is True
    decision = service.got["decisions"]["c1"]
    assert decision["approve"] is False
    assert decision["reason"] == "用户停止了回答"


def test_stream_rejected_while_pending():
    service = ResumeService(pending=True)
    with pytest.raises(HTTPException) as exc:
        anyio.run(
            agent_stream,
            AgentChatRequest(question="你好", conversation_id=3),
            service,
        )
    assert exc.value.status_code == 409


def test_stream_rejected_while_conversation_active():
    stop = threading.Event()
    run_id = register_active_run(stop, conversation_id=42)
    try:
        with pytest.raises(HTTPException) as exc:
            anyio.run(
                agent_stream,
                AgentChatRequest(question="你好", conversation_id=42),
                ResumeService(pending=False),
            )
        assert exc.value.status_code == 409
    finally:
        unregister_active_run(run_id)


def test_pending_endpoint_returns_ops_or_empty():
    from app.api.agent import agent_pending_approval

    res = agent_pending_approval(5, ResumeService(pending=True))
    assert res["pending"] is True
    assert res["ops"][0]["call_id"] == "c1"
    assert res["conversation_id"] == 5
    assert res["plan"] is None

    res2 = agent_pending_approval(5, ResumeService(pending=False))
    assert res2["pending"] is False
    assert res2["ops"] == []
    assert res2["plan"] is None


PLAN_PAYLOAD = {
    "kind": "approval",
    "created_at": 2.0,
    "ops": [],
    "plan": {
        "call_id": "p1",
        "steps": ["看代码", "写文件"],
        "summary": "两步行事",
    },
}


def test_plan_suspension_payload_exposed():
    from app.api.agent import agent_pending_approval

    res = agent_pending_approval(9, ResumeService(payload=PLAN_PAYLOAD))
    assert res["pending"] is True
    assert res["plan"]["steps"] == ["看代码", "写文件"]


def test_resume_passes_plan_decision_and_defaults_to_reject():
    service = ResumeService(payload=PLAN_PAYLOAD)

    async def scenario():
        response = await agent_resume(
            AgentResumeRequest(
                conversation_id=9,
                plan=AgentPlanDecision(approved=True, steps=["看代码", "写文件（改）"]),
            ),
            service,
        )
        return await _drain(response)

    anyio.run(scenario)
    assert service.got["plan"]["approved"] is True
    assert service.got["plan"]["steps"] == ["看代码", "写文件（改）"]

    # 停止且未提交计划决定 → 服务端补"拒绝"
    service2 = ResumeService(payload=PLAN_PAYLOAD)

    async def scenario2():
        response = await agent_resume(
            AgentResumeRequest(conversation_id=9, stopped=True), service2
        )
        return await _drain(response)

    frames = anyio.run(scenario2)
    assert service2.got["plan"]["approved"] is False
    assert service2.got["plan"]["reason"] == "用户停止了回答"
    assert any(f["event"] == "done" for f in frames)
