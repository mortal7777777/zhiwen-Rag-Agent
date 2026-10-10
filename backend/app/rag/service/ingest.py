"""RAG 服务 · 入库与索引账本（从 rag/service.py 拆出，2026-10-10）。

ensure_index 全量/增量重建、manifest/指纹账本、Parent-Child 切分。"""


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

class IngestMixin:
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
