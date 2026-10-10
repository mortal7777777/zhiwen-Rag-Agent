"""快捷切换的缓存语义（refresh_chat_only）：只清对话相关缓存。

聊天页高频切换供应商时若走全量 refresh()，每次都会让下一次检索重新加载
本地 BGE（体感数秒）。这组用例用对象哨兵钉住"重资源缓存不被清"的不变量。
"""

from __future__ import annotations

from app.agent.agent import AgentService
from app.rag.service import RAGService


def test_rag_refresh_chat_only_keeps_heavy_caches():
    svc = RAGService.__new__(RAGService)
    embeddings = object()
    reranker = object()
    dedup = object()
    chat = object()
    expander = object()
    svc._embeddings = embeddings
    svc._reranker = reranker
    svc._dedup = dedup
    svc._chat = chat
    svc._query_expander = expander

    svc.refresh_chat_only()

    # 对话缓存被清（切换后生效）
    assert svc._chat is None
    assert svc._query_expander is None
    # 重资源缓存原样保留（对象同一性，不是"被重建"）
    assert svc._embeddings is embeddings
    assert svc._reranker is reranker
    assert svc._dedup is dedup


def test_agent_refresh_chat_only_keeps_vision():
    svc = AgentService.__new__(AgentService)
    vision = object()
    svc._chat = object()
    svc._chat_plain = object()
    svc._title_chat = object()
    svc._context = object()
    svc._vision = vision

    svc.refresh_chat_only()

    assert svc._chat is None
    assert svc._chat_plain is None
    assert svc._title_chat is None
    assert svc._context is None
    assert svc._vision is vision
