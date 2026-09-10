"""RAG 服务：加载、Parent-Child 切分、嵌入、存储、检索、重排、生成串成完整流程。

这是后端的核心编排层，所有 API 都通过它访问 RAG 能力。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import UploadFile
from langchain_core.documents import Document

from ..config import Settings
from ..monitoring import metrics
from .embeddings import LocalBGEEmbeddings
from .loader import SUPPORTED_SUFFIXES, list_data_files, load_documents
from .llm import DeepSeekChat
from .query_expander import QueryExpander
from .reranker import LocalReranker
from .retriever import hybrid_search, merge_query_results, small_to_big
from .splitter import build_parent_child_splitters
from .store import IDX_SCHEMA_VERSION, OpenSearchStore, slugify
from ..runtime_config import chat_provider_config, effective

logger = logging.getLogger(__name__)


class RAGService:
    """知问 ZhiWen 的 RAG 核心服务。"""

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

    # ============================================================
    # 懒加载组件
    # ============================================================
    @property
    def embeddings(self) -> LocalBGEEmbeddings:
        """嵌入模型：provider=api 且配了 base_url 时用 API，否则本地 BGE。"""
        if self._embeddings is None:
            from ..runtime_config import effective
            from .embeddings import APIBGEEmbeddings

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
            from ..runtime_config import effective
            from .reranker import APIReranker

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
            from .vision import SenseNovaVision

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
            self._chat = DeepSeekChat(
                api_key=cfg.get("api_key"),
                base_url=cfg.get("base_url") or "https://api.deepseek.com",
                model=cfg.get("model") or "deepseek-v4-flash",
                thinking_enabled=cfg.get("thinking_enabled", True),
                thinking_effort=cfg.get("thinking_effort", "high"),
            )
        return self._chat

    def refresh(self) -> None:
        """设置变更后重置懒加载缓存（嵌入/重排、模型、查询扩展器、视觉客户端）。"""
        self._embeddings = None
        self._reranker = None
        self._chat = None
        self._query_expander = None
        self._vision = None

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
        """兼容旧接口：整体问答许可（等价 GPU 许可）。"""
        self._semaphore.acquire()

    def release(self) -> None:
        """兼容旧接口：释放整体问答许可。"""
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

    # ============================================================
    # 文件指纹账本（增量索引）
    # ============================================================
    def _index_meta_dir(self) -> Path:
        """每个索引独立的账本目录，避免不同索引互相覆盖。"""
        return self.settings.meta_dir / self.settings.opensearch_index

    def _manifest_file(self) -> Path:
        return self._index_meta_dir() / "manifest.json"

    def _chunks_file(self) -> Path:
        return self._index_meta_dir() / "chunks.json"

    def _compute_file_fingerprints(self) -> dict[str, str]:
        """为 data/ 下每个文件生成指纹 {相对路径: SHA-256(大小+修改时间)}。"""
        fingerprints: dict[str, str] = {}
        for file_path in list_data_files(self.settings.data_dir):
            stat = file_path.stat()
            raw = f"{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
            fingerprints[file_path.relative_to(self.settings.data_dir).as_posix()] = (
                hashlib.sha256(raw).hexdigest()
            )
        return fingerprints

    def _save_meta(
        self,
        file_fps: dict[str, str],
        parents: list[Document],
        child_count: int,
    ) -> None:
        """保存文件指纹账本和 parent 副本（OpenSearch 才是主存储）。"""
        self._index_meta_dir().mkdir(parents=True, exist_ok=True)
        self._manifest_file().write_text(
            json.dumps(
                {
                    "files": file_fps,
                    "parent_count": len(parents),
                    "child_count": child_count,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._chunks_file().write_text(
            json.dumps(
                {
                    "chunks": [
                        {"page_content": doc.page_content, "metadata": doc.metadata}
                        for doc in parents
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _load_parents(self) -> list[Document] | None:
        """读取本地 parent 副本（增量追加时复用）；缺失或损坏时返回 None。"""
        if not self._chunks_file().exists():
            return None
        try:
            data = json.loads(self._chunks_file().read_text(encoding="utf-8"))
            return [
                Document(
                    page_content=item["page_content"],
                    metadata=item.get("metadata", {}),
                )
                for item in data["chunks"]
            ]
        except Exception:
            return None

    def _split_parent_child(
        self,
        documents: list[Document],
    ) -> tuple[list[Document], list[Document]]:
        """把文档切成 parent（段落分组，上下文）和 child（小窗口，定位精度）。

        块级去重（作用域=单文件）：同一文件内重复的 child 只保留首次出现的一份。
        跨文件重复保留各自副本——per-doc 删除要求删任一文件不牵连其他文件；
        检索端的重复占坑由 source 分流接管（见 docs/INDEX_REDESIGN_PLAN.md §1.4）。
        """
        parents: list[Document] = []
        children: list[Document] = []
        skipped = 0
        # 去重作用域 = 单文件（PDF 分页 / EPUB 分章节属于同一文件，共用去重集合）；
        # 跨文件重复保留各自副本（per-doc 删除才互不牵连）
        seen_by_file: dict[str, set[str]] = {}
        for document in documents:
            file_key = str(
                document.metadata.get("relative_path")
                or document.metadata.get("source")
                or ""
            )
            seen_child_md5 = seen_by_file.setdefault(file_key, set())
            for parent_text in self._parent_splitter.split_text(document.page_content):
                parent_id = hashlib.md5(parent_text.encode("utf-8")).hexdigest()
                parent_meta = dict(document.metadata)
                parent_meta["parent_id"] = parent_id
                parents.append(Document(page_content=parent_text, metadata=parent_meta))

                for child_text in self._child_splitter.split_text(parent_text):
                    child_md5 = hashlib.md5(child_text.encode("utf-8")).hexdigest()
                    if child_md5 in seen_child_md5:
                        skipped += 1
                        continue
                    seen_child_md5.add(child_md5)
                    child_meta = dict(parent_meta)
                    child_meta["parent_content"] = parent_text
                    children.append(
                        Document(page_content=child_text, metadata=child_meta)
                    )
        if skipped:
            logger.info("块级去重：跳过 %d 个重复 child", skipped)
        return parents, children

    # ============================================================
    # 索引构建（per-doc 运维，见 docs/INDEX_REDESIGN_PLAN.md）
    # ============================================================
    def _physical_base(self) -> str:
        """当前配置对应的物理索引名前缀：{逻辑名}_{模型}_{维度}_v{schema}。"""
        provider = str(effective(self.settings, "embedding_provider") or "local")
        if provider == "api":
            model = str(effective(self.settings, "embedding_api_model") or "api_embed")
        else:
            model = self.settings.embedding_model_dir.name
        return (
            f"{self.settings.opensearch_index}_{slugify(model)}"
            f"_{self.embeddings.dimension}_v{IDX_SCHEMA_VERSION}"
        )

    def _read_manifest(self) -> dict | None:
        path = self._manifest_file()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def ensure_index(self, force_rebuild: bool = False) -> OpenSearchStore:
        """切分 -> 本地嵌入 -> 写入 OpenSearch（per-doc 增量 + 别名蓝绿重建）。

        指纹 diff 驱动（docs/INDEX_REDESIGN_PLAN.md §1.5）：
          1. 无变化            -> 直接复用；
          2. 新增/修改/删除    -> 按文件粒度应用（删除零嵌入、修改只重嵌该文件）；
          3. 首次/换模型/改切分/强制/账本缺失 -> 蓝绿全量重建（新物理索引 + 原子切别名）。
        """
        with self._index_lock:
            self.settings.ensure_dirs()
            store = self.store
            store.ping()
            file_fps = self._compute_file_fingerprints()
            old_fps = (self._read_manifest() or {}).get("files", {})

            write_index, via_alias = store.resolve()
            base = self._physical_base()
            managed = bool(write_index) and via_alias and write_index.startswith(base)

            # 裸物理索引（旧脚本显式指定索引名）：保持历史行为，不做别名管理
            if write_index and not via_alias:
                return self._ensure_legacy(store, file_fps, old_fps, force_rebuild)

            # 情况 1：指纹命中 -> 直接复用
            if (
                not force_rebuild
                and managed
                and old_fps
                and file_fps == old_fps
                and store.doc_count() > 0
            ):
                logger.info("命中 OpenSearch 索引缓存，直接复用")
                return store

            # 情况 2：全量重建（首次/别名缺失/换模型/改 schema/强制/账本缺失）
            need_rebuild = force_rebuild or not managed
            if not need_rebuild and not old_fps:
                if not file_fps and store.doc_count() == 0:
                    return store  # 空知识库是合法稳态（等待首次上传）
                need_rebuild = True
            if need_rebuild:
                self._full_rebuild(store, file_fps)
                return store

            # 情况 3：增量（按文件 diff）
            added = [k for k in file_fps if k not in old_fps]
            changed = [k for k in old_fps if k in file_fps and old_fps[k] != file_fps[k]]
            removed = [k for k in old_fps if k not in file_fps]
            if added or changed or removed:
                self._apply_incremental(store, file_fps, added, changed, removed)
            return store

    def _apply_incremental(
        self,
        store: OpenSearchStore,
        file_fps: dict[str, str],
        added: list[str],
        changed: list[str],
        removed: list[str],
    ) -> None:
        """按文件粒度应用差异：删/改先删旧块，增/改单独嵌入追加，最后同步账本。"""
        parents = self._load_parents()
        if parents is None:
            logger.warning("parent 副本账本缺失，降级为全量重建")
            self._full_rebuild(store, file_fps)
            return

        for rel in removed + changed:
            deleted = store.delete_by_document(rel)
            if deleted:
                logger.info("已删除文件 %s 的 %d 个旧块", rel, deleted)
            else:
                logger.warning(
                    "删除 %s 命中 0 块（若为旧版 schema 索引，请手动全量重建）", rel
                )
        dropped = set(removed) | set(changed)
        if dropped:
            parents = [
                p
                for p in parents
                if str(p.metadata.get("relative_path") or "") not in dropped
            ]

        to_embed = added + changed
        if to_embed:
            docs = load_documents(
                self.settings.data_dir, set(to_embed), **self._loader_kwargs()
            )
            new_parents, new_children = self._split_parent_child(docs)
            logger.info(
                "增量索引：%d 个新增/修改文件，%d 个块",
                len(to_embed),
                len(new_children),
            )
            if new_children:
                store.bulk_index(
                    new_children,
                    self.embeddings,
                    batch_size=self.settings.embed_batch_size,
                )
            parents.extend(new_parents)

        self._save_meta(file_fps, parents, child_count=store.doc_count())

    def _full_rebuild(
        self,
        store: OpenSearchStore,
        file_fps: dict[str, str],
        use_alias: bool = True,
    ) -> None:
        """全量重建。别名模式 = 蓝绿：写新物理索引 -> 原子切别名 -> 清理旧索引。"""
        documents = load_documents(self.settings.data_dir, **self._loader_kwargs())
        if not documents:
            raise RuntimeError(
                f"数据目录 {self.settings.data_dir} 没有可索引的文档，请先上传文件"
            )
        parents, children = self._split_parent_child(documents)
        logger.info(
            "切分为 %d 个 parent、%d 个 child，开始本地嵌入",
            len(parents),
            len(children),
        )
        if use_alias:
            base = self._physical_base()
            physical = store.create_physical_index(
                base, dimension=self.embeddings.dimension
            )
            try:
                store.bulk_index(
                    children,
                    self.embeddings,
                    batch_size=self.settings.embed_batch_size,
                    target=physical,
                )
                store.swap_alias(physical, alias=self.settings.opensearch_index)
            except Exception:
                # 失败清理新索引，别名仍指向旧索引（不影响检索）
                store.delete_index(physical)
                raise
            self._save_meta(file_fps, parents, child_count=len(children))
            for old in store.list_physical_indices(base):
                if old == physical:
                    continue
                try:
                    store.delete_index(old)
                    logger.info("已清理旧物理索引 %s", old)
                except Exception as exc:
                    logger.warning("清理旧物理索引 %s 失败：%s", old, exc)
            logger.info(
                "索引重建完成：别名 %s → %s",
                self.settings.opensearch_index,
                physical,
            )
        else:
            if store.index_exists():
                store.delete_index()
            store.create_index(
                self.settings.opensearch_index, dimension=self.embeddings.dimension
            )
            store.bulk_index(
                children,
                self.embeddings,
                batch_size=self.settings.embed_batch_size,
            )
            self._save_meta(file_fps, parents, child_count=len(children))

    def _ensure_legacy(
        self,
        store: OpenSearchStore,
        file_fps: dict[str, str],
        old_fps: dict[str, str],
        force_rebuild: bool,
    ) -> OpenSearchStore:
        """裸物理索引（非别名）：保持历史语义——指纹命中复用，否则原地重建。"""
        if (
            not force_rebuild
            and old_fps
            and file_fps == old_fps
            and store.doc_count() > 0
        ):
            return store
        logger.info("索引 %s 为裸物理索引，原地全量重建", self.settings.opensearch_index)
        self._full_rebuild(store, file_fps, use_alias=False)
        return store

    # ============================================================
    # 问答
    # ============================================================
    def retrieve(
        self,
        question: str,
        history: list[dict] | None = None,
        rewrite: bool | None = None,
        parent_child: bool | None = None,
    ) -> list[Document]:
        """检索流水线：查询扩展 -> 混合检索 -> 跨查询 RRF -> Parent 聚合 -> 精排。

        - rewrite：是否启用查询扩展（Multi-Query/HyDE/多轮补全，默认取配置）；
        - parent_child：是否启用 Parent-Child（默认取配置）。
        """
        store = self.ensure_index()
        use_parent_child = (
            self.settings.parent_child_enabled if parent_child is None else parent_child
        )

        use_expansion = (
            self.settings.query_expansion_enabled if rewrite is None else rewrite
        )
        queries = [question]
        if use_expansion:
            start = time.perf_counter()
            try:
                queries = self.query_expander.expand(question, history)
            except Exception as exc:
                logger.warning("查询扩展失败，退化为原问题：%s", exc)
                queries = [question]
            metrics.record("expansion", time.perf_counter() - start)
            logger.info("查询扩展（%d 条）：%s", len(queries), queries)

        start = time.perf_counter()
        per_query = [
            hybrid_search(
                store,
                self.embeddings,
                query,
                recall_k=self.settings.recall_k,
                candidate_pool=self.settings.candidate_pool,
            )
            for query in queries
        ]
        candidates = merge_query_results(per_query, limit=40)
        if use_parent_child:
            candidates = small_to_big(candidates, self.settings.max_parents)
        docs = self.reranker.rerank(
            question,
            candidates,
            top_k=self.settings.rerank_top_k,
        )
        metrics.record("retrieval", time.perf_counter() - start)
        return docs

    def ask(self, question: str, history: list[dict] | None = None) -> dict:
        """完整 RAG 流程：检索 -> 精排 -> 生成，返回回答与引用来源。"""
        metrics.inc("chat_requests")
        try:
            docs = self.retrieve(question, history=history)
            context = "\n\n".join(doc.page_content for doc in docs)
            start = time.perf_counter()
            answer = self.chat.generate(question, context, history)
            metrics.record("generation", time.perf_counter() - start)
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
            return {"answer": answer, "sources": sources}
        except Exception:
            metrics.inc("chat_errors")
            raise

    # ============================================================
    # 文档管理
    # ============================================================
    def list_documents(self) -> list[dict]:
        """列出 data/ 下的知识库文件。"""
        result = []
        for path in list_data_files(self.settings.data_dir):
            stat = path.stat()
            result.append(
                {
                    "name": path.name,
                    "relative_path": path.relative_to(self.settings.data_dir).as_posix(),
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(
                        timespec="seconds"
                    ),
                }
            )
        return result

    def add_documents(self, files: list[UploadFile]) -> list[dict]:
        """保存上传文件到 data/ 并增量更新索引。"""
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        for file in files:
            # 只取文件名部分，防止路径穿越（../ 等）
            filename = Path(file.filename or "未命名").name
            suffix = Path(filename).suffix.lower()
            if suffix not in SUPPORTED_SUFFIXES:
                supported = " / ".join(sorted(SUPPORTED_SUFFIXES))
                raise ValueError(
                    f"不支持的文件格式：{filename}（当前支持：{supported}）"
                )
            target = self.settings.data_dir / filename
            target.write_bytes(file.file.read())
            logger.info("已保存文件：%s", filename)
        self.ensure_index()
        return self.list_documents()

    def delete_document(self, relative_path: str) -> None:
        """删除文件并同步移除其向量块（per-doc：指纹 diff 驱动，无需全量重建）。"""
        data_dir = self.settings.data_dir.resolve()
        target = (data_dir / relative_path).resolve()
        if data_dir not in target.parents:
            raise ValueError("非法的文件路径")
        if not target.exists():
            raise FileNotFoundError(f"文件不存在：{relative_path}")
        target.unlink()
        logger.info("已删除文件：%s", relative_path)
        self.ensure_index()

    def rebuild_index(self) -> dict:
        """强制重建索引。"""
        self.ensure_index(force_rebuild=True)
        return self.index_status()

    def index_status(self) -> dict:
        """返回索引状态：向量条数、文本块数、数据目录、别名指向的物理索引。"""
        manifest: dict = self._read_manifest() or {}
        error: str | None = None
        doc_count = 0
        physical: str | None = None
        try:
            if self.store.index_exists():
                physical = self.store.resolve_write_index()
                doc_count = self.store.doc_count()
        except Exception as exc:
            error = str(exc)
        return {
            "index_name": self.settings.opensearch_index,
            "physical_index": physical,
            "doc_count": doc_count,
            "chunk_count": manifest.get("child_count", 0),
            "parent_count": manifest.get("parent_count", 0),
            "data_dir": str(self.settings.data_dir),
            "error": error,
        }
