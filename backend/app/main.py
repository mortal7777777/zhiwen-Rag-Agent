"""FastAPI 应用入口。

启动方式（在 backend 目录下）：
    python -m uvicorn app.main:app --reload --port 8000
或直接运行：
    python run.py
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.agent import router as agent_router
from .api.agent_types import router as agent_types_router
from .api.advanced import router as advanced_router
from .api.chat import router as chat_router
from .api.conversations import router as conversations_router
from .api.documents import router as documents_router
from .api.fs import router as fs_router
from .api.health import router as health_router
from .api.memories import router as memories_router
from .api.prompt_admin import router as prompt_admin_router
from .api.runs import router as runs_router
from .api.settings import router as settings_router
from .api.skills import router as skills_router
from .api.suggestions import router as suggestions_router
from .api.vision import router as vision_router
from .api_auth import API_TOKEN_EXEMPT_PATHS, ApiTokenMiddleware
from .config import get_settings
from .db.database import init_db
from .logging_config import RequestContextMiddleware, configure_logging
from .rag.service import IndexRebuildInProgress
from .runtime_config import load_overrides
from .single_instance import acquire_instance_lock

configure_logging()
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期：启动时准备目录，关闭时清理。"""
    settings.ensure_dirs()
    # 单实例契约（P0）：会话并发闸门 / 审批超时 watchdog / 挂起用量快照都在
    # 进程内存里，两个进程共享同一 meta_dir 会静默破坏"挂起期间拒绝新消息"
    # 与"超时自动拒绝"的语义。拿不到锁直接拒绝启动（见 app/single_instance.py）。
    instance_lock = acquire_instance_lock(settings.meta_dir)
    init_db()  # MySQL：连接失败会降级，不影响服务启动
    # 加载用户运行时配置（模型/供应商覆盖）
    try:
        from .db.database import SessionLocal, db_ready

        if db_ready and SessionLocal is not None:
            with SessionLocal() as session:
                load_overrides(session)
    except Exception as exc:
        logger.warning("加载运行时配置失败：%s", exc)

    def _warmup_rag_models() -> None:
        """后台预热本地 BGE embedding/reranker（懒加载模型 → CUDA）。

        不预热时，重启/热重载后的首次知识库检索要付 30~60s 的模型加载，
        用户看到的就是"第一问特别慢"（如 run#208 的 56s 检索）。
        放到启动期后台做，首轮检索直接命中热模型。
        """
        import time as _time

        def _run() -> None:
            try:
                from langchain_core.documents import Document

                from .api.deps import get_service

                service = get_service()
                # 预热也走 GPU 许可：release_gpu 会按空闲 TTL 安排卸载，
                # 否则重启后无请求时预热模型会永久占用显存。
                service.acquire_gpu()
                try:
                    t0 = _time.perf_counter()
                    service.embeddings.embed_query("预热")
                    t1 = _time.perf_counter()
                    service.reranker.rerank(
                        "预热", [Document(page_content="预热")], top_k=1
                    )
                    logger.info(
                        "RAG 模型预热完成：embedding %.1fs + reranker %.1fs（设备 %s/%s）",
                        t1 - t0,
                        _time.perf_counter() - t1,
                        service.embeddings.device,
                        service.reranker.device,
                    )
                finally:
                    service.release_gpu()
            except Exception as exc:
                logger.warning("RAG 模型预热失败（首次检索时将懒加载）：%s", exc)

        threading.Thread(target=_run, name="rag-warmup", daemon=True).start()

    _warmup_rag_models()

    # 附件清理：上传时也会清一次；长期不上传的实例靠每小时后台清理兜底
    attachment_stop = threading.Event()

    def _attachment_cleanup_loop() -> None:
        from .attachments import cleanup_expired

        while not attachment_stop.is_set():
            try:
                cleanup_expired(settings)
            except Exception as exc:  # noqa: BLE001 - 清理失败不阻断服务
                logger.warning("附件定时清理失败：%s", exc)
            if attachment_stop.wait(3600):
                return

    threading.Thread(
        target=_attachment_cleanup_loop, name="attachment-cleanup", daemon=True
    ).start()
    logger.info("启动 %s v%s", settings.app_name, settings.version)
    if not settings.api_token:
        # 机制兜底：README 声明"单用户本机项目"，但部署到局域网/公网前必须
        # 让"忘了配认证"在日志里显式可见（EXPOSE_DOCS=0 只藏文档，不拦请求）
        logger.warning(
            "未设置 ZHIWEN_API_TOKEN：API 完全无认证（仅限单用户本机使用）；"
            "对外/局域网部署务必设置该环境变量，并经网关提供 HTTPS"
        )
    try:
        yield
    finally:
        attachment_stop.set()
        try:
            from .api.deps import shutdown_agent_service

            shutdown_agent_service()
        except Exception as exc:  # noqa: BLE001 - 退出清理失败不阻断关闭
            logger.warning("关闭 Agent/MCP 会话失败：%s", exc)
        instance_lock.release()
        logger.info("服务已关闭")


app = FastAPI(
    title=settings.app_name,
    description=(
        "个人知识库 + 智能 Agent：本地 BGE Embedding / Reranker + OpenSearch + "
        "DeepSeek（ReAct 工具调用、联网搜索、MySQL 对话记忆、预设提示词模板）"
    ),
    version=settings.version,
    lifespan=lifespan,
    # 对外部署可设 EXPOSE_DOCS=0 关闭 /docs 与 /openapi.json
    docs_url="/docs" if settings.expose_docs else None,
    redoc_url="/redoc" if settings.expose_docs else None,
    openapi_url="/openapi.json" if settings.expose_docs else None,
)

# 可选全局鉴权（ZHIWEN_API_TOKEN）：必须在 CORSMiddleware 之前 add——
# add_middleware 后加者在外层，CORS 位于外层时浏览器预检 OPTIONS（不携带
# Authorization）才能由 CORS 直接应答，实际请求仍会被本中间件校验。
if settings.api_token:
    app.add_middleware(
        ApiTokenMiddleware,
        token=settings.api_token,
        exempt_paths=API_TOKEN_EXEMPT_PATHS,
    )
    logger.info(
        "API Token 认证已启用（放行探针：%s）",
        ", ".join(sorted(API_TOKEN_EXEMPT_PATHS)),
    )

# 前端（Vite 开发服务器）跨域配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware)


@app.exception_handler(IndexRebuildInProgress)
async def index_rebuild_in_progress_handler(
    request: Request, exc: IndexRebuildInProgress
) -> JSONResponse:
    """后台重建/索引维护期间拒绝写操作（避免请求被重建锁挂死）。"""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理：记录 traceback，返回带 request_id 的 500（不泄露细节）。"""
    request_id = getattr(request.state, "request_id", None)
    logger.exception(
        "未处理异常 request_id=%s %s %s", request_id, request.method, request.url.path
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误", "request_id": request_id},
    )

app.include_router(health_router, prefix="/api")
app.include_router(fs_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(agent_router, prefix="/api")
app.include_router(agent_types_router, prefix="/api")
app.include_router(advanced_router, prefix="/api")
app.include_router(conversations_router, prefix="/api")
app.include_router(prompt_admin_router, prefix="/api")
app.include_router(memories_router, prefix="/api")
app.include_router(vision_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(suggestions_router, prefix="/api")
app.include_router(runs_router, prefix="/api")
app.include_router(skills_router, prefix="/api")


@app.get("/")
def root() -> dict:
    """根路径：提示去 /docs 看接口文档。"""
    return {"message": settings.app_name, "docs": "/docs"}
