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
from .store import OpenSearchStore
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

        跨文档块级去重：内容 md5 已见过的 child 直接跳过（重复段落主要来自
        同一书内反复出现的段落），保留首次出现的 parent 上下文归属。
        """
        parents: list[Document] = []
        children: list[Document] = []
        seen_child_md5: set[str] = set()
        skipped = 0
        for document in documents:
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
    # 索引构建
    # ============================================================
    def ensure_index(self, force_rebuild: bool = False) -> OpenSearchStore:
        """切分 -> 本地嵌入 -> 写入 OpenSearch，支持增量更新。

        三种情况：
          1. 文件都没变        -> 直接复用 OpenSearch 索引；
          2. 只新增了文件      -> 只嵌入新文件并追加；
          3. 有修改/删除/强制  -> 删除旧索引，全量重建。
        """
        self.settings.ensure_dirs()
        store = self.store
        store.ping()
        file_fps = self._compute_file_fingerprints()

        manifest: dict | None = None
        if self._manifest_file().exists():
            try:
                manifest = json.loads(self._manifest_file().read_text(encoding="utf-8"))
            except Exception:
                manifest = None
        old_fps = (manifest or {}).get("files", {})

        # 情况 1：数据没变且索引存在 -> 直接复用
        if (
            not force_rebuild
            and store.index_exists()
            and old_fps
            and file_fps == old_fps
            and store.doc_count() > 0
        ):
            logger.info("命中 OpenSearch 索引缓存，直接复用")
            return store

        added = [k for k in file_fps if k not in old_fps]
        changed = [k for k in old_fps if k in file_fps and old_fps[k] != file_fps[k]]
        removed = [k for k in old_fps if k not in file_fps]
        old_parents = self._load_parents()

        # 情况 2：只有新增文件 -> 增量追加
        if (
            not force_rebuild
            and store.index_exists()
            and old_fps
            and added
            and not changed
            and not removed
            and old_parents is not None
        ):
            logger.info("检测到 %d 个新文件，增量嵌入并追加", len(added))
            new_docs = load_documents(
                self.settings.data_dir, set(added), **self._loader_kwargs()
            )
            new_parents, new_children = self._split_parent_child(new_docs)
            store.bulk_index(
                new_children,
                self.embeddings,
                batch_size=self.settings.embed_batch_size,
            )
            self._save_meta(
                file_fps,
                old_parents + new_parents,
                child_count=store.doc_count(),
            )
            return store

        # 情况 3：修改/删除/强制 -> 全量重建
        if force_rebuild:
            logger.info("强制重建索引")
        elif store.index_exists():
            logger.info(
                "检测到 %d 个文件修改、%d 个文件删除，重建索引",
                len(changed),
                len(removed),
            )
        else:
            logger.info("首次运行，创建索引")

        if store.index_exists():
            store.delete_index()
        store.create_index()

        documents = load_documents(
            self.settings.data_dir, **self._loader_kwargs()
        )
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
        store.bulk_index(
            children,
            self.embeddings,
            batch_size=self.settings.embed_batch_size,
        )
        self._save_meta(file_fps, parents, child_count=len(children))
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
        """删除文件并重建索引（删除会影响旧向量，全量重建最稳妥）。"""
        data_dir = self.settings.data_dir.resolve()
        target = (data_dir / relative_path).resolve()
        if data_dir not in target.parents:
            raise ValueError("非法的文件路径")
        if not target.exists():
            raise FileNotFoundError(f"文件不存在：{relative_path}")
        target.unlink()
        logger.info("已删除文件：%s", relative_path)
        self.ensure_index(force_rebuild=True)

    def rebuild_index(self) -> dict:
        """强制重建索引。"""
        self.ensure_index(force_rebuild=True)
        return self.index_status()

    def index_status(self) -> dict:
        """返回索引状态：向量条数、文本块数、数据目录。"""
        manifest: dict = {}
        if self._manifest_file().exists():
            try:
                manifest = json.loads(self._manifest_file().read_text(encoding="utf-8"))
            except Exception:
                manifest = {}
        error: str | None = None
        doc_count = 0
        try:
            if self.store.index_exists():
                doc_count = self.store.doc_count()
        except Exception as exc:
            error = str(exc)
        return {
            "index_name": self.settings.opensearch_index,
            "doc_count": doc_count,
            "chunk_count": manifest.get("child_count", 0),
            "parent_count": manifest.get("parent_count", 0),
            "data_dir": str(self.settings.data_dir),
            "error": error,
        }
