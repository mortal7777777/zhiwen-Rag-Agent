"""RAG 服务 · 基座（从 rag/service.py 拆出，2026-10-10）。

懒加载单例（嵌入/重排/视觉/存储/对话模型）、并发锁（GPU / LLM）、
检索入口 retrieve_locked 与 UploadOutcome 结果类型。mixins 只允许依赖本模块。"""


from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from fastapi import UploadFile
from langchain_core.documents import Document

from ...config import Settings
from ...monitoring import metrics
from ..embeddings import LocalBGEEmbeddings
from ..loader import SUPPORTED_SUFFIXES, list_data_files, load_documents
from ..llm import DeepSeekChat
from ..query_expander import QueryExpander
from ..reranker import LocalReranker
from ..retriever import (
    TITLE_RERANK_BONUS,
    TOC_RERANK_PENALTY,
    book_scoped_children,
    ensure_parents,
    extract_titles,
    hybrid_search,
    is_enumeration_question,
    is_toc_like,
    match_book_paths,
    merge_query_results,
    small_to_big,
    title_matches_source,
)
from ..splitter import build_parent_child_splitters
from ..store import IDX_SCHEMA_VERSION, OpenSearchStore, slugify
from ..dedup import DedupChecker
from ..versioning import archive_file, validate_version_no
from ...runtime_config import chat_provider_config, effective
from contextlib import contextmanager
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass
class UploadOutcome:
    """add_documents 的结果：mode = dry_run（仅检测）| conflict（待确认）| committed（已落盘）。"""

    mode: str
    documents: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    archived: list[dict] = field(default_factory=list)


class RAGServiceBase:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._parent_splitter, self._child_splitter = build_parent_child_splitters(
            parent_chunk_size=settings.parent_chunk_size,
            parent_max_chunk_size=settings.parent_max_chunk_size,
            child_chunk_size=settings.child_chunk_size,
            child_overlap=settings.child_overlap,
        )
        # 以下组件全部懒加载：首次使用时才初始化
        self._embeddings: LocalBGEEmbeddings | None = None
        self._reranker: LocalReranker | None = None
        self._store: OpenSearchStore | None = None
        self._chat: DeepSeekChat | None = None
        self._query_expander: QueryExpander | None = None
        self._vision = None
        # 限制并发问答数量，保护 GPU（embedding/reranker）不被并发请求打满
        self._semaphore = threading.BoundedSemaphore(settings.max_concurrency)
        # LLM API 调用并发上限（生成阶段不占 GPU 锁，单独限流）
        self._llm_semaphore = threading.BoundedSemaphore(settings.llm_max_concurrency)
        # 索引维护单写者锁：上传/删除/重建/检索触发的增量维护在此串行
        self._index_lock = threading.RLock()
        # 上传查重缓存（按当前 data_dir 惰性构建；data_dir 变化时重建）
        self._dedup: DedupChecker | None = None
        self._dedup_dir = None


    @property
    def embeddings(self) -> LocalBGEEmbeddings:
        """嵌入模型：provider=api 且配了 base_url 时用 API，否则本地 BGE。"""
        if self._embeddings is None:
            from ...runtime_config import effective
            from ..embeddings import APIBGEEmbeddings

            provider = str(effective(self.settings, "embedding_provider") or "local")
            base_url = str(effective(self.settings, "embedding_api_base_url") or "").strip()
            if provider == "api" and base_url:
                self._embeddings = APIBGEEmbeddings(
                    base_url=base_url,
                    api_key=str(effective(self.settings, "embedding_api_key") or "").strip(),
                    model=str(effective(self.settings, "embedding_api_model") or "").strip(),
                )
                logger.info(
                    "Embedding 使用 API：%s（模型 %s）",
                    base_url,
                    effective(self.settings, "embedding_api_model") or "供应商默认",
                )
            else:
                self._embeddings = LocalBGEEmbeddings(
                    self.settings.embedding_model_dir,
                    use_fp16=self.settings.embedding_fp16,
                )
                logger.info(
                    "本地 Embedding 模型就绪：%s（%s，%d 维）",
                    self.settings.embedding_model_dir.name,
                    self._embeddings.device,
                    self._embeddings.dimension,
                )
        return self._embeddings


    @property
    def reranker(self) -> LocalReranker:
        """重排序模型：provider=api 且配了 base_url 时用 API，否则本地 BGE。"""
        if self._reranker is None:
            from ...runtime_config import effective
            from ..reranker import APIReranker

            provider = str(effective(self.settings, "reranker_provider") or "local")
            base_url = str(effective(self.settings, "reranker_api_base_url") or "").strip()
            if provider == "api" and base_url:
                self._reranker = APIReranker(
                    base_url=base_url,
                    api_key=str(effective(self.settings, "reranker_api_key") or "").strip(),
                    model=str(effective(self.settings, "reranker_api_model") or "").strip(),
                )
                logger.info(
                    "Reranker 使用 API：%s（模型 %s）",
                    base_url,
                    effective(self.settings, "reranker_api_model") or "供应商默认",
                )
            else:
                self._reranker = LocalReranker(self.settings.reranker_cache_dir)
        return self._reranker


    @property
    def vision(self):
        """SenseNova 视觉客户端（懒加载；未配 key 时 configured=False）。"""
        if self._vision is None:
            from ..vision import SenseNovaVision

            self._vision = SenseNovaVision(self.settings)
        return self._vision


    def _loader_kwargs(self) -> dict:
        """文档加载参数：布局感知 PDF + 可选扫描页 OCR。"""
        kwargs = {
            "layout_pdf": self.settings.pdf_layout_enabled,
            "pdf_workers": self.settings.pdf_parse_workers,
        }
        if (
            self.settings.pdf_vision_ocr_enabled
            and self.vision.configured
        ):
            kwargs["vision"] = self.vision
            kwargs["vision_ocr_pages"] = self.settings.pdf_vision_ocr_max_pages
        return kwargs


    @property
    def store(self) -> OpenSearchStore:
        if self._store is None:
            self._store = OpenSearchStore(
                url=self.settings.opensearch_url,
                index_name=self.settings.opensearch_index,
                dimension=self.embeddings.dimension,
            )
        return self._store


    @property
    def chat(self) -> DeepSeekChat:
        if self._chat is None:
            cfg = chat_provider_config(self.settings) or {}
            if not cfg.get("api_key"):
                raise RuntimeError("未配置对话模型 API Key，无法生成回答")
            self._chat = DeepSeekChat(cfg)
        return self._chat


    def refresh(self) -> None:
        """设置变更后重置懒加载缓存（嵌入/重排、模型、查询扩展器、视觉客户端）。"""
        self._embeddings = None
        self._reranker = None
        self._chat = None
        self._query_expander = None
        self._vision = None
        self._dedup = None

    def refresh_chat_only(self) -> None:
        """只重置对话生成缓存（聊天页快捷切换供应商）。

        **不碰** `_embeddings/_reranker/_dedup`——全量 refresh() 会让下一次检索
        重新加载本地 BGE（体感数秒），快捷切换不该付这个代价。
        """
        self._chat = None
        self._query_expander = None


    @property
    def dedup(self) -> DedupChecker:
        """上传查重器（哈希/签名缓存；data_dir 变化时重建）。"""
        current = self.settings.data_dir
        if self._dedup is None or self._dedup_dir != current:
            self._dedup = DedupChecker(current)
            self._dedup_dir = current
        return self._dedup


    @contextmanager
    def index_lock(self):
        """索引维护单写者锁的公开入口（归档/改名/恢复等编排复用，RLock 可重入）。"""
        with self._index_lock:
            yield


    @property
    def query_expander(self) -> QueryExpander:
        """查询扩展器：Multi-Query + HyDE + 多轮补全。"""
        if self._query_expander is None:
            self._query_expander = QueryExpander(
                chat=self.chat,
                variants=self.settings.rewrite_variants,
                use_multi_query=self.settings.expansion_multi_query,
                use_hyde=self.settings.expansion_hyde,
                use_multi_turn=self.settings.expansion_multi_turn,
            )
        return self._query_expander


    def acquire(self) -> None:
        """【已废弃】整体问答许可（等价 GPU 许可）。

        2026-10-08 起不要在新代码里用：把许可握到生成结束会阻塞其他会话的
        检索。检索请用 `retrieve_locked()`（自持自放），生成段用
        `acquire_llm()/release_llm()`。保留仅为兼容外部脚本。
        """
        self._semaphore.acquire()


    def release(self) -> None:
        """【已废弃】释放整体问答许可（见 `acquire()` 说明）。"""
        self._semaphore.release()


    def acquire_gpu(self) -> None:
        """获取 GPU 许可（embedding / rerank 段）。"""
        self._semaphore.acquire()


    def release_gpu(self) -> None:
        """释放 GPU 许可。"""
        self._semaphore.release()


    def acquire_llm(self) -> None:
        """获取 LLM API 调用许可。"""
        self._llm_semaphore.acquire()


    def release_llm(self) -> None:
        """释放 LLM API 调用许可。"""
        self._llm_semaphore.release()


    def retrieve_locked(
        self,
        question: str,
        history: list[dict] | None = None,
    ) -> list[Document]:
        """检索段持 GPU 锁、返回后立即释放（生成段不持锁）。

        2026-10-08 与 Agent 路径统一口径：旧实现由 API 层用 `acquire()` 把
        整个问答（含生成）锁住，一个 /api/chat 生成会占着 GPU 并发名额，
        拖慢其他会话的检索。锁的边界=embedding/rerank 真正用到 GPU 的那段。
        """
        self.acquire_gpu()
        try:
            return self.retrieve(question, history=history)
        finally:
            self.release_gpu()
