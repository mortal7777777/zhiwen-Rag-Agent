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
from ..schemas import AgentChatRequest, AgentChatResponse
from .deps import get_agent_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["agent"])

# 正在运行的流式请求注册表：run_id -> stop_event。
# 客户端 Ctrl+C 时通过 /agent/cancel/{run_id} 主动置停止信号，
# 让后台线程立即收尾（不必等 SSE 生成器感知连接断开）。
_ACTIVE_RUNS: dict[str, threading.Event] = {}
_RUNS_LOCK = threading.Lock()


def register_active_run(stop_event: threading.Event) -> str:
    """登记一次流式运行，返回 run_id。"""
    run_id = uuid.uuid4().hex
    with _RUNS_LOCK:
        _ACTIVE_RUNS[run_id] = stop_event
    return run_id


def unregister_active_run(run_id: str) -> None:
    with _RUNS_LOCK:
        _ACTIVE_RUNS.pop(run_id, None)


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


@router.post("/agent/stream")
async def agent_stream(
    request: AgentChatRequest,
    service: AgentService = Depends(get_agent_service),
) -> StreamingResponse:
    """SSE 流式 Agent：session -> tool_start/tool_result -> token... -> done。"""

    async def event_generator():
        queue: Queue = Queue()
        stop_event = threading.Event()
        run_id = register_active_run(stop_event)
        # SSE 心跳：工具执行可能 10~20 秒无事件，注释行让前端/代理知道连接存活
        HEARTBEAT_INTERVAL = 15

        def worker():
            try:
                for event in service.run(
                    question=request.question,
                    use_web_search=request.use_web_search,
                    use_knowledge_base=request.use_knowledge_base,
                    conversation_id=request.conversation_id,
                    template_id=request.template_id,
                    system_prompt=request.system_prompt,
                    tool_mode=request.tool_mode,
                    stop_event=stop_event,
                    images=request.images,
                    plan_only=request.plan_only,
                    resume_plan=request.resume_plan,
                    project_dir=request.project_dir,
                    command_sandbox=request.command_sandbox,
                ):
                    queue.put(event)
                    # 注意：不要在这里 break，否则生成器被提前丢弃，
                    # _run 内部的停止检查会自行收尾（保存问题、跳过半截回答）
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
