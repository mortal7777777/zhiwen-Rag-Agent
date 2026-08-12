"""FastAPI 应用入口。

启动方式（在 backend 目录下）：
    python -m uvicorn app.main:app --reload --port 8000
或直接运行：
    python run.py
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.agent import router as agent_router
from .api.advanced import router as advanced_router
from .api.chat import router as chat_router
from .api.conversations import router as conversations_router
from .api.documents import router as documents_router
from .api.health import router as health_router
from .api.memories import router as memories_router
from .api.runs import router as runs_router
from .api.settings import router as settings_router
from .api.skills import router as skills_router
from .api.suggestions import router as suggestions_router
from .api.templates import router as templates_router
from .api.vision import router as vision_router
from .config import get_settings
from .db.database import init_db
from .runtime_config import load_overrides

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期：启动时准备目录，关闭时清理。"""
    settings.ensure_dirs()
    init_db()  # MySQL：连接失败会降级，不影响服务启动
    # 加载用户运行时配置（模型/供应商覆盖）
    try:
        from .db.database import SessionLocal, db_ready

        if db_ready and SessionLocal is not None:
            with SessionLocal() as session:
                load_overrides(session)
    except Exception as exc:
        logger.warning("加载运行时配置失败：%s", exc)
    logger.info("启动 %s v%s", settings.app_name, settings.version)
    yield
    logger.info("服务已关闭")


app = FastAPI(
    title=settings.app_name,
    description=(
        "个人知识库 + 智能 Agent：本地 BGE Embedding / Reranker + OpenSearch + "
        "DeepSeek（ReAct 工具调用、联网搜索、MySQL 对话记忆、预设提示词模板）"
    ),
    version=settings.version,
    lifespan=lifespan,
)

# 前端（Vite 开发服务器）跨域配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(agent_router, prefix="/api")
app.include_router(advanced_router, prefix="/api")
app.include_router(conversations_router, prefix="/api")
app.include_router(templates_router, prefix="/api")
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
