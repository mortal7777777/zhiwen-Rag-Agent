"""Agent 接口：普通 JSON 模式 + SSE 流式模式。"""

from __future__ import annotations

import json
import logging
import threading
import asyncio
import uuid
from queue import Queue

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..agent.agent import AgentService
from ..db import get_db
from ..db import repository as repo
from ..schemas import AgentChatRequest, AgentChatResponse, AgentResumeRequest
from .deps import get_agent_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["agent"])

# 正在运行的流式请求注册表：run_id -> stop_event。
# 客户端 Ctrl+C 时通过 /agent/cancel/{run_id} 主动置停止信号，
# 让后台线程立即收尾（不必等 SSE 生成器感知连接断开）。
_ACTIVE_RUNS: dict[str, threading.Event] = {}
# 会话 -> run_id：并发闸门（同会话同时只允许一条流，防检查点状态互相覆盖）
_ACTIVE_CONVS: dict[int, str] = {}
_RUNS_LOCK = threading.Lock()


def register_active_run(
    stop_event: threading.Event, conversation_id: int | None = None
) -> str:
    """登记一次流式运行，返回 run_id。"""
    run_id = uuid.uuid4().hex
    with _RUNS_LOCK:
        _ACTIVE_RUNS[run_id] = stop_event
        if conversation_id is not None:
            _ACTIVE_CONVS[conversation_id] = run_id
    return run_id


def unregister_active_run(run_id: str) -> None:
    with _RUNS_LOCK:
        _ACTIVE_RUNS.pop(run_id, None)
        stale = [cid for cid, rid in _ACTIVE_CONVS.items() if rid == run_id]
        for cid in stale:
            _ACTIVE_CONVS.pop(cid, None)


def is_conversation_active(conversation_id: int | None) -> bool:
    """该会话是否有正在进行的流式运行。"""
    if conversation_id is None:
        return False
    with _RUNS_LOCK:
        return conversation_id in _ACTIVE_CONVS


def request_cancel(run_id: str) -> tuple[bool, str]:
    """置运行停止信号；返回 (是否成功, 失败原因)。"""
    with _RUNS_LOCK:
        stop_event = _ACTIVE_RUNS.get(run_id)
    if stop_event is None:
        return False, "run not found"
    stop_event.set()
    return True, ""


def _sse(payload: dict) -> str:
    """SSE 事件帧：data: {json}\n\n"""
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@router.post("/agent/chat", response_model=AgentChatResponse)
def agent_chat(
    request: AgentChatRequest,
    service: AgentService = Depends(get_agent_service),
) -> dict:
    """非流式 Agent 问答：工具调用（按需）+ 记忆 + 最终回答。"""
    _reject_if_busy(request.conversation_id, service)
    try:
        return service.run_json(
            question=request.question,
            use_web_search=request.use_web_search,
            use_knowledge_base=request.use_knowledge_base,
            conversation_id=request.conversation_id,
            template_id=request.template_id,
            system_prompt=request.system_prompt,
            tool_mode=request.tool_mode,
            images=request.images,
            plan_only=request.plan_only,
            resume_plan=request.resume_plan,
            project_dir=request.project_dir,
            command_sandbox=request.command_sandbox,
        )
    except Exception as exc:
        logger.exception("Agent 问答失败")
        raise HTTPException(status_code=500, detail=f"问答失败：{exc}")


def _reject_if_busy(conversation_id: int | None, service: AgentService) -> None:
    """挂起中或已有运行中的会话：拒绝新消息（防检查点状态被覆盖）。"""
    has_pending = getattr(service, "has_pending_approval", None)
    if callable(has_pending) and has_pending(conversation_id):
        raise HTTPException(
            status_code=409,
            detail="该会话有待确认的操作，请先批准或拒绝后再发送新消息",
        )
    if is_conversation_active(conversation_id):
        raise HTTPException(status_code=409, detail="该会话已有正在进行的回复")


def _sse_response(make_iterator, conversation_id: int | None) -> StreamingResponse:
    """SSE 管道：后台线程跑事件生成器，心跳保活，断连置停止信号。

    make_iterator(stop_event) 返回事件迭代器（service.run 或 service.resume）。
    """

    async def event_generator():
        queue: Queue = Queue()
        stop_event = threading.Event()
        run_id = register_active_run(stop_event, conversation_id)
        # SSE 心跳：工具执行可能 10~20 秒无事件，注释行让前端/代理知道连接存活
        HEARTBEAT_INTERVAL = 15

        def worker():
            try:
                # 注意：不要提前 break，否则生成器被提前丢弃，
                # 停止检查由生成器内部自行收尾（保存问题、跳过半截回答）
                for event in make_iterator(stop_event):
                    queue.put(event)
            except Exception as exc:
                logger.exception("Agent 流式执行失败")
                queue.put({"event": "error", "data": {"message": str(exc)}})
            finally:
                queue.put(None)

        threading.Thread(target=worker, daemon=True).start()

        async def _get_event():
            """带超时的取事件；超时返回心跳标记，queue 结束返回 None。"""
            try:
                return await asyncio.to_thread(queue.get, timeout=HEARTBEAT_INTERVAL)
            except Exception:
                return "__heartbeat__"

        try:
            while True:
                event = await asyncio.wait_for(
                    _get_event(),
                    timeout=HEARTBEAT_INTERVAL + 5,
                )
                if event == "__heartbeat__":
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    break
                # session 事件里带上 run_id，客户端可据此取消本次生成
                if event.get("event") == "session":
                    event = {
                        **event,
                        "data": {**(event.get("data") or {}), "run_id": run_id},
                    }
                yield _sse(event)
        finally:
            # 客户端断开连接时置停止信号，让后台线程尽快退出
            stop_event.set()
            unregister_active_run(run_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/agent/stream")
async def agent_stream(
    request: AgentChatRequest,
    service: AgentService = Depends(get_agent_service),
) -> StreamingResponse:
    """SSE 流式 Agent：session -> tool_start/tool_result -> token... -> done。

    敏感操作会以 run_suspended 收尾（本次流结束）；客户端提交决定后
    经 /agent/resume 开新流续跑同一条回答线。
    """
    _reject_if_busy(request.conversation_id, service)
    return _sse_response(
        lambda stop: service.run(
            question=request.question,
            use_web_search=request.use_web_search,
            use_knowledge_base=request.use_knowledge_base,
            conversation_id=request.conversation_id,
            template_id=request.template_id,
            system_prompt=request.system_prompt,
            tool_mode=request.tool_mode,
            stop_event=stop,
            images=request.images,
            plan_only=request.plan_only,
            resume_plan=request.resume_plan,
            project_dir=request.project_dir,
            command_sandbox=request.command_sandbox,
        ),
        request.conversation_id,
    )


@router.post("/agent/resume")
async def agent_resume(
    request: AgentResumeRequest,
    service: AgentService = Depends(get_agent_service),
) -> StreamingResponse:
    """恢复挂起的运行（敏感操作审批）：新开 SSE 流续跑同一条回答线。"""
    resume_fn = getattr(service, "resume", None)
    pending_fn = getattr(service, "has_pending_approval", None)
    payload_fn = getattr(service, "pending_approval_payload", None)
    if resume_fn is None or pending_fn is None:
        raise HTTPException(status_code=400, detail="当前服务不支持恢复挂起的运行")
    if not pending_fn(request.conversation_id):
        raise HTTPException(status_code=409, detail="该会话没有待确认的操作")
    if is_conversation_active(request.conversation_id):
        raise HTTPException(status_code=409, detail="该会话已有正在进行的回复")

    decisions: dict[str, dict] = {
        d.call_id: {
            "approve": bool(d.approve),
            "reason": (d.reason or "").strip()[:200],
            "remember_session": bool(d.remember_session),
            "remember_forever": bool(d.remember_forever),
        }
        for d in request.decisions
        if d.call_id
    }
    plan_payload = (
        {
            "approved": bool(request.plan.approved),
            "steps": [str(s).strip() for s in (request.plan.steps or []) if str(s).strip()],
            "reason": (request.plan.reason or "").strip()[:200],
        }
        if request.plan
        else None
    )
    # 缺省项与「停止」：以拒绝补全（停止的理由与旧阻塞式路径一致）
    payload = payload_fn(request.conversation_id) if callable(payload_fn) else None
    for op in (payload or {}).get("ops") or []:
        cid = str(op.get("call_id") or "")
        if cid and cid not in decisions:
            decisions[cid] = {
                "approve": False,
                "reason": (
                    "用户停止了回答" if request.stopped else "未提交决定，已按拒绝处理"
                ),
            }
    if (payload or {}).get("plan") and plan_payload is None:
        plan_payload = {
            "approved": False,
            "steps": [],
            "reason": (
                "用户停止了回答" if request.stopped else "未提交计划决定，已按拒绝处理"
            ),
        }

    return _sse_response(
        lambda stop: resume_fn(
            conversation_id=request.conversation_id,
            decisions=decisions,
            stopped=bool(request.stopped),
            plan=plan_payload,
            db=None,
            stop_event=stop,
        ),
        request.conversation_id,
    )


@router.get("/agent/pending/{conversation_id}")
def agent_pending_approval(
    conversation_id: int,
    service: AgentService = Depends(get_agent_service),
) -> dict:
    """该会话挂起中的审批载荷（刷新/切换会话后前端据此恢复审批卡）。"""
    payload_fn = getattr(service, "pending_approval_payload", None)
    payload = payload_fn(conversation_id) if callable(payload_fn) else None
    if not payload:
        return {
            "pending": False,
            "conversation_id": conversation_id,
            "ops": [],
            "plan": None,
        }
    return {
        "pending": True,
        "conversation_id": conversation_id,
        "ops": payload.get("ops") or [],
        "plan": payload.get("plan") or None,
    }


@router.post("/agent/cancel/{run_id}")
async def agent_cancel(run_id: str) -> dict:
    """主动取消某次正在进行的流式生成（CLI 按 Ctrl+C 时调用）。"""
    cancelled, reason = request_cancel(run_id)
    return {"run_id": run_id, "cancelled": cancelled, "reason": reason}


@router.get("/agent/context/{conversation_id}")
def agent_context(
    conversation_id: int,
    service: AgentService = Depends(get_agent_service),
    db=Depends(get_db),
) -> dict:
    """上下文占用统计：消息条数 / token 预算 / 滚动摘要进度（/context 命令）。"""
    if repo.get_conversation(db, conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        return service.context.context_stats(db, conversation_id)
    except Exception as exc:
        logger.exception("上下文统计失败")
        raise HTTPException(status_code=500, detail=f"统计失败：{exc}")


@router.post("/agent/compact/{conversation_id}")
def agent_compact(
    conversation_id: int,
    service: AgentService = Depends(get_agent_service),
    db=Depends(get_db),
) -> dict:
    """手动压缩会话历史（/compact）：保留近期消息，更早的并入滚动摘要。"""
    if repo.get_conversation(db, conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        return service.context.compact_now(db, conversation_id)
    except Exception as exc:
        logger.exception("手动压缩失败")
        raise HTTPException(status_code=500, detail=f"压缩失败：{exc}")
