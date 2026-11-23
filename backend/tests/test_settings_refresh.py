"""设置保存后的 RAG 缓存刷新粒度：只有检索基础设施键才全量 refresh。"""

from __future__ import annotations

from app.api.settings import _needs_full_rag_refresh


def test_chat_only_settings_do_not_reload_local_models():
    assert not _needs_full_rag_refresh({"chat_temperature": 0.3})
    assert not _needs_full_rag_refresh(
        {"memory_consolidate_threshold": 20, "kb_chat_min_score": 0.4}
    )
    assert not _needs_full_rag_refresh({})


def test_retrieval_infra_settings_require_full_refresh():
    assert _needs_full_rag_refresh({"data_dir": "D:/docs"})
    assert _needs_full_rag_refresh({"embedding_provider": "api"})
    assert _needs_full_rag_refresh({"reranker_api_model": "bge-reranker-v2-m3"})
