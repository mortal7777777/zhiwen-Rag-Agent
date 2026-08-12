"""问答接口：普通 JSON 模式 + SSE 流式模式。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..monitoring import metrics
from ..rag.service import RAGService
from ..schemas import ChatRequest, ChatResponse
from .deps import get_service

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, service: RAGService = Depends(get_service)) -> ChatResponse:
    """RAG 问答：检索知识库 -> 本地重排序 -> DeepSeek 生成回答。"""
    service.acquire()  # 并发限制，保护 GPU
    try:
        result = service.ask(
            question=request.question,
            history=[message.model_dump() for message in request.history],
        )
        return ChatResponse(**result)
    except RuntimeError as exc:
        # 配置缺失（如 DEEPSEEK_API_KEY）或数据目录为空
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"问答失败：{exc}")
    finally:
        service.release()


def _sse(payload: dict) -> str:
    """SSE 事件帧：data: {json}\n\n"""
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    service: RAGService = Depends(get_service),
) -> StreamingResponse:
    """SSE 流式问答：先返回 sources 事件，再逐 token 返回回答。"""

    async def event_generator():
        history = [message.model_dump() for message in request.history]
        try:
            # 并发许可在事件循环里获取会阻塞，放到线程池
            await run_in_threadpool(service.acquire)
            metrics.inc("stream_requests")

            # 检索（含查询扩展/重排）是同步重活，在线程池执行
            docs = await run_in_threadpool(
                service.retrieve,
                request.question,
                history,
            )
            context = "\n\n".join(doc.page_content for doc in docs)
            sources = [
                {
                    "content": doc.page_content,
                    "score": doc.metadata.get("rerank_score")
                    or doc.metadata.get("parent_score"),
                    "source": doc.metadata.get("source"),
                    "page": doc.metadata.get("page"),
                }
                for doc in docs
            ]
            yield _sse({"event": "sources", "data": sources})

            # 流式生成：逐 token 推送
            async for token in service.chat.astream(
                request.question,
                context,
                history,
            ):
                yield _sse({"event": "token", "data": token})

            yield _sse({"event": "done", "data": {"ok": True}})
        except Exception as exc:
            metrics.inc("stream_errors")
            yield _sse({"event": "error", "data": str(exc)})
        finally:
            service.release()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 防止反向代理缓冲 SSE
        },
    )
