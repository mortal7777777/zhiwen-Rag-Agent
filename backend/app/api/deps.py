"""依赖注入：整个应用共享同一个 RAGService 实例。"""

from __future__ import annotations

from functools import lru_cache

from ..agent.langgraph_agent import LangGraphAgentService
from ..config import get_settings
from ..rag.service import RAGService


@lru_cache
def get_service() -> RAGService:
    """返回全局唯一的 RAGService（内部组件均为懒加载）。"""
    return RAGService(get_settings())


@lru_cache
def get_agent_service() -> LangGraphAgentService:
    """返回全局唯一的 LangGraph 编排版 AgentService（包装 RAGService）。"""
    return LangGraphAgentService(get_settings(), get_service())


def shutdown_agent_service() -> None:
    """仅当 AgentService 已创建时关闭其 MCP 会话，避免退出时反向实例化。"""
    if not get_agent_service.cache_info().currsize:
        return
    service = get_agent_service()
    shutdown = getattr(service, "shutdown", None)
    if callable(shutdown):
        shutdown()
