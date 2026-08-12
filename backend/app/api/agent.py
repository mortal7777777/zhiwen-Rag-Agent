"""Agent 接口：普通 JSON 模式 + SSE 流式模式。"""

from __future__ import annotations

import json
import logging
import threading
import asyncio
from queue import Queue

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..agent.agent import AgentService
from ..schemas import AgentChatRequest, AgentChatResponse
from .deps import get_agent_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["agent"])


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
                yield _sse(event)
        finally:
            # 客户端断开连接时置停止信号，让后台线程尽快退出
            stop_event.set()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
