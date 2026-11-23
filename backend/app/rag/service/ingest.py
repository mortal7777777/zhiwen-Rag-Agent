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
from ..loader import (
    PARSE_SCHEMA_VERSION,
    SUPPORTED_SUFFIXES,
    list_data_files,
    load_documents,
)
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
from ..ir import (
    build_parents,
    parse_page_blocks,
    split_table_children,
    strip_image_markers,
)
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
        parse_reports: dict | None = None,
        parse_version: str | None = None,
    ) -> None:
        """保存文件指纹账本和 parent 副本（OpenSearch 才是主存储）。

        parse_version=None 表示刚做过全量重建，写当前解析器版本；增量/单文件
        重解析必须传入旧版本，否则会把"解析器升级待重建"标记清掉，整库混合
        新旧解析结果却不再提示重建。
        """
        self._index_meta_dir().mkdir(parents=True, exist_ok=True)
        self._manifest_file().write_text(
            json.dumps(
                {
                    "files": file_fps,
                    "parent_count": len(parents),
                    "child_count": child_count,
                    "parse_version": (
                        PARSE_SCHEMA_VERSION if parse_version is None else parse_version
                    ),
                    "parse_reports": parse_reports or {},
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
        """把文档切成 parent 与 child。

        PDF 页文档走 IR：标题/题目边界 + 跨页续接 + 表格原子块；
        其他格式保持“每 Document 段落分组”的旧行为。去重作用域仍=单文件。
        """
        parents: list[Document] = []
        children: list[Document] = []
        skipped = 0
        seen_by_file: dict[str, set[str]] = {}
        groups: list[tuple[str, list[Document]]] = []
        for document in documents:
            file_key = str(
                document.metadata.get("relative_path")
                or document.metadata.get("source")
                or ""
            )
            if not groups or groups[-1][0] != file_key:
                groups.append((file_key, [document]))
            else:
                groups[-1][1].append(document)

        for file_key, group in groups:
            seen_child_md5 = seen_by_file.setdefault(file_key, set())
            if self._use_ir(group):
                group_parents, group_children, group_skipped = self._split_pdf_ir(
                    group, seen_child_md5
                )
            else:
                group_parents, group_children, group_skipped = (
                    self._split_documents_legacy(group, seen_child_md5)
                )
            parents.extend(group_parents)
            children.extend(group_children)
            skipped += group_skipped

        if skipped:
            logger.info("块级去重：跳过 %d 个重复 child", skipped)
        return parents, children


    @staticmethod
    def _use_ir(group: list[Document]) -> bool:
        if not group:
            return False
        if not str(group[0].metadata.get("source") or "").lower().endswith(".pdf"):
            return False
        return all(document.metadata.get("page") is not None for document in group)


    def _split_documents_legacy(
        self,
        documents: list[Document],
        seen_child_md5: set[str],
    ) -> tuple[list[Document], list[Document], int]:
        parents: list[Document] = []
        children: list[Document] = []
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
        return parents, children, skipped


    def _split_pdf_ir(
        self,
        group: list[Document],
        seen_child_md5: set[str],
    ) -> tuple[list[Document], list[Document], int]:
        blocks = []
        # 按页码排序：稀疏页 VLM OCR / 其它补页逻辑可能把页文档追加到列表末尾，
        # 直接按列表顺序建块会让跨页 parent 把末页与更早的页拼在一起
        for document in sorted(
            group, key=lambda doc: int(doc.metadata.get("page") or 0)
        ):
            page = int(document.metadata.get("page") or 1)
            blocks.extend(parse_page_blocks(document.page_content, page))
        if not blocks:
            return self._split_documents_legacy(group, seen_child_md5)

        layout = str(group[0].metadata.get("doc_layout") or "book")
        parent_blocks = build_parents(
            blocks,
            target=self.settings.parent_chunk_size,
            max_size=self.settings.parent_max_chunk_size,
            layout=layout,
        )
        image_map: dict[str, dict] = {}
        for document in group:
            for image in document.metadata.get("image_refs") or []:
                image_id = str(image.get("id") or "")
                if image_id and image_id not in image_map:
                    image_map[image_id] = image

        base_meta = dict(group[0].metadata)
        backends = {
            str(document.metadata.get("parse_backend") or "") for document in group
        }
        backends.discard("")
        backend = "paddle" if "paddle" in backends else (sorted(backends)[0] if backends else None)

        parents: list[Document] = []
        children: list[Document] = []
        skipped = 0
        for parent_block in parent_blocks:
            parent_meta = dict(base_meta)
            parent_meta.pop("image_refs", None)
            parent_meta.update(
                {
                    "page": parent_block.page,
                    "pages": parent_block.pages,
                    "heading_path": parent_block.heading_path,
                    "block_kinds": parent_block.block_kinds,
                    "is_table": parent_block.is_table,
                }
            )
            if backend:
                parent_meta["parse_backend"] = backend
            image_refs = [
                image_map[image_id]
                for image_id in parent_block.image_ids
                if image_id in image_map
            ]
            if image_refs:
                parent_meta["image_refs"] = image_refs
            # 标记只用于定位图片：图片信息已在 image_refs 元数据里，正文去掉标记
            text = strip_image_markers(parent_block.text) or parent_block.text
            parent_id = hashlib.md5(text.encode("utf-8")).hexdigest()
            parent_meta["parent_id"] = parent_id
            parents.append(
                Document(page_content=text, metadata=parent_meta)
            )

            if parent_block.is_table:
                child_texts = split_table_children(
                    text, self.settings.child_chunk_size
                )
                if not child_texts:
                    child_texts = self._child_splitter.split_text(text)
            else:
                child_texts = self._child_splitter.split_text(text)
            for child_text in child_texts:
                child_md5 = hashlib.md5(child_text.encode("utf-8")).hexdigest()
                if child_md5 in seen_child_md5:
                    skipped += 1
                    continue
                seen_child_md5.add(child_md5)
                child_meta = dict(parent_meta)
                child_meta["parent_content"] = text
                children.append(
                    Document(page_content=child_text, metadata=child_meta)
                )
        return parents, children, skipped


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


    def parse_upgrade_pending(self) -> bool:
        """解析器版本与账本不一致：需显式后台重建，写路径暂不更新索引。"""
        manifest = self._read_manifest() or {}
        return bool(manifest) and (
            manifest.get("parse_version") != PARSE_SCHEMA_VERSION
        )


    def ensure_index(self, force_rebuild: bool = False) -> OpenSearchStore:
        """校验/维护索引；成功后刷新检索侧短路时间戳（写路径也走这里）。

        后台重建期间直接返回当前索引（蓝绿重建完成前旧别名仍可检索），
        避免上传/检索请求被数小时的重建锁阻塞。
        """
        if self._rebuild_state.get("running"):
            return self.store
        old_manifest = self._read_manifest() or {}
        parse_changed = bool(old_manifest) and (
            old_manifest.get("parse_version") != PARSE_SCHEMA_VERSION
        )
        if parse_changed and not force_rebuild:
            # 解析器升级需要数小时，不能在检索/上传请求线程里同步重建；
            # 标记 stale（见 index_health），由 POST /api/index/rebuild 后台执行。
            logger.warning("解析器版本已变化：需后台重建索引，当前继续使用旧索引")
            return self.store
        store = self._run_ensure_index(force_rebuild)
        self._last_ensured_at = time.monotonic()
        return store


    def rebuild_state(self) -> dict:
        return dict(self._rebuild_state)


    def reparse_file(self, relative_path: str) -> dict:
        """只重新解析/重嵌一个文件（缓存命中优先；用于重试 fallback 页）。"""
        with self.index_lock():
            file_fps = self._compute_file_fingerprints()
            if relative_path not in file_fps:
                raise FileNotFoundError(f"文件不存在：{relative_path}")
            old_manifest = self._read_manifest() or {}
            old_fps = old_manifest.get("files") or {}
            store = self.store
            store.ping()
            if relative_path in old_fps:
                self._apply_incremental(
                    store, file_fps, old_fps, added=[], changed=[relative_path], removed=[]
                )
            else:
                self._apply_incremental(
                    store, file_fps, old_fps, added=[relative_path], changed=[], removed=[]
                )
            self._last_ensured_at = time.monotonic()
            report = (
                (self._read_manifest() or {}).get("parse_reports", {}).get(relative_path)
            )
            return {"ok": True, "path": relative_path, "report": report}


    def start_background_rebuild(self, force: bool = True) -> dict:
        """启动后台重建线程（解析器升级后必须显式触发，或等下一次强制重建）。"""
        if self._rebuild_state.get("running"):
            return {
                "started": False,
                "reason": "already_running",
                **self.rebuild_state(),
            }
        # 重建线程会长期持有 _index_lock：这里不能无限排队等锁
        if not self._index_lock.acquire(timeout=5.0):
            return {
                "started": False,
                "reason": "index_busy",
                **self.rebuild_state(),
            }
        try:
            if self._rebuild_state.get("running"):
                return {
                    "started": False,
                    "reason": "already_running",
                    **self.rebuild_state(),
                }
            self._rebuild_state = {
                "running": True,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": None,
                "error": None,
            }
        finally:
            self._index_lock.release()

        def _run() -> None:
            try:
                self._run_ensure_index(True)
                self._last_ensured_at = time.monotonic()
                self._rebuild_state.update(
                    {
                        "finished_at": datetime.now().isoformat(timespec="seconds"),
                        "error": None,
                    }
                )
                logger.info("后台索引重建完成")
            except Exception as exc:  # noqa: BLE001
                logger.exception("后台索引重建失败")
                self._rebuild_state.update(
                    {
                        "finished_at": datetime.now().isoformat(timespec="seconds"),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            finally:
                self._rebuild_state["running"] = False

        self._rebuild_thread = threading.Thread(
            target=_run, name="index-rebuild", daemon=True
        )
        self._rebuild_thread.start()
        return {"started": True, **self.rebuild_state()}

    def ingest_state(self) -> dict:
        """后台增量索引状态（/api/index/health 的 ingest 字段）。"""
        return dict(self._ingest_state)

    def start_background_ingest(self, reason: str = "upload") -> dict:
        """启动后台增量索引线程（上传/改名/恢复版本的大文件解析+嵌入）。

        与 start_background_rebuild 同款模式：HTTP 请求只负责落盘/移动文件，
        重活交给后台线程；进度经 ingest_state()（/api/index/health）暴露。
        - 已有 ingest 在跑：置 rerun 标记，当前线程跑完后再补一轮（新落盘
          的文件可能晚于本轮指纹计算）；
        - 后台重建进行中：不启动（index_busy），重建完成后可走 /api/index/refresh。
        """
        if self._rebuild_state.get("running"):
            return {
                "started": False,
                "skip_reason": "index_busy",
                **self.ingest_state(),
            }
        if self._ingest_state.get("running"):
            self._ingest_state["rerun"] = True
            return {
                "started": False,
                "skip_reason": "already_running",
                **self.ingest_state(),
            }
        self._ingest_state = {
            "running": True,
            "reason": reason,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
            "error": None,
        }

        def _run() -> None:
            try:
                while True:
                    # 与公开 ensure_index 相同的前置守卫（重建中/解析器待升级不硬来），
                    # 但取锁用阻塞获取：后台线程不该被 5s 超时静默跳过
                    if self._rebuild_state.get("running"):
                        self._ingest_state.update(
                            finished_at=datetime.now().isoformat(timespec="seconds"),
                            error="后台重建进行中，本次增量索引未执行（完成后可点「应用增量」）",
                        )
                        return
                    manifest = self._read_manifest() or {}
                    if manifest and manifest.get("parse_version") != PARSE_SCHEMA_VERSION:
                        self._ingest_state.update(
                            finished_at=datetime.now().isoformat(timespec="seconds"),
                            error=None,
                            note="解析器待重建：本次增量索引跳过（需先全量重建）",
                        )
                        return
                    with self._index_lock:
                        self._run_ensure_index_locked(False)
                    self._last_ensured_at = time.monotonic()
                    if not self._ingest_state.get("rerun"):
                        self._ingest_state.update(
                            finished_at=datetime.now().isoformat(timespec="seconds"),
                            error=None,
                        )
                        return
                    # 期间又有新文件落盘：再补一轮指纹 diff
                    self._ingest_state["rerun"] = False
            except Exception as exc:  # noqa: BLE001 - 失败写入状态，不抛出线程
                logger.exception("后台增量索引失败")
                self._ingest_state.update(
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                    error=f"{type(exc).__name__}: {exc}",
                )
            finally:
                self._ingest_state["running"] = False

        self._ingest_thread = threading.Thread(
            target=_run, name="index-ingest", daemon=True
        )
        self._ingest_thread.start()
        return {"started": True, **self.ingest_state()}


    def _run_ensure_index(self, force_rebuild: bool = False) -> OpenSearchStore:
        """索引维护入口：限时抢锁，避免被后台重建的长期持锁挂住。"""
        if self._rebuild_state.get("running") and not force_rebuild:
            return self.store
        if not self._index_lock.acquire(timeout=5.0):
            logger.warning(
                "索引维护锁被长时间占用（后台重建进行中？），本次跳过索引维护"
            )
            return self.store
        try:
            return self._run_ensure_index_locked(force_rebuild)
        finally:
            self._index_lock.release()


    def _run_ensure_index_locked(self, force_rebuild: bool = False) -> OpenSearchStore:
        """切分 -> 本地嵌入 -> 写入 OpenSearch（per-doc 增量 + 别名蓝绿重建）。

        指纹 diff 驱动（docs/INDEX_REDESIGN_PLAN.md §1.5）：
          1. 无变化            -> 直接复用；
          2. 新增/修改/删除    -> 按文件粒度应用（删除零嵌入、修改只重嵌该文件）；
          3. 首次/换模型/改切分/强制/账本缺失 -> 蓝绿全量重建（新物理索引 + 原子切别名）。
        """
        # 调用方（_run_ensure_index / 已持锁的写路径）保证串行；RLock 同线程重入立即返回
        with self._index_lock:
            self.settings.ensure_dirs()
            store = self.store
            store.ping()
            file_fps = self._compute_file_fingerprints()
            old_manifest = self._read_manifest() or {}
            old_fps = old_manifest.get("files", {})
            # 解析器版本变化必须全量重建：文件指纹不变时，否则会永远复用旧解析产物
            parse_changed = bool(old_manifest) and (
                old_manifest.get("parse_version") != PARSE_SCHEMA_VERSION
            )
            if parse_changed:
                logger.info(
                    "解析器版本变化（%s → %s），触发全量重建",
                    old_manifest.get("parse_version") or "legacy",
                    PARSE_SCHEMA_VERSION,
                )

            write_index, via_alias = store.resolve()
            base = self._physical_base()
            managed = bool(write_index) and via_alias and write_index.startswith(base)

            # 裸物理索引（旧脚本显式指定索引名）：保持历史行为，不做别名管理
            if write_index and not via_alias:
                return self._ensure_legacy(store, file_fps, old_fps, force_rebuild)

            # 情况 1：指纹命中 -> 直接复用（解析器版本一致才允许）
            if (
                not force_rebuild
                and not parse_changed
                and managed
                and old_fps
                and file_fps == old_fps
                and store.doc_count() > 0
            ):
                logger.info("命中 OpenSearch 索引缓存，直接复用")
                return store

            # 情况 2：全量重建（首次/别名缺失/换模型/改 schema/改解析器/强制/账本缺失）
            need_rebuild = force_rebuild or not managed or parse_changed
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
                self._apply_incremental(
                    store, file_fps, old_fps, added, changed, removed
                )
            return store


    def ensure_index_cached(self, ttl: float = 60.0) -> OpenSearchStore:
        """检索侧入口：ttl 内跳过 ensure_index 的 ping + 全目录扫描 + 别名查询。

        在线检索每次都做一次全目录 stat/SHA256 + 3 次 OpenSearch HTTP 太贵。
        写路径（上传/删除/重建/工具写入）仍直接调 ensure_index()，变更立即
        生效；只有绕过服务直接改 data/ 目录时，最多延迟 ttl 秒被发现。
        """
        if self._rebuild_state.get("running"):
            return self.store  # 蓝绿重建写新索引，旧别名继续服务检索
        now = time.monotonic()
        if now - self._last_ensured_at < ttl:
            return self.store
        # 重建线程长期持锁：限时抢锁失败就直接用当前索引，绝不排队等重建
        if not self._index_lock.acquire(timeout=5.0):
            logger.warning("索引维护锁被占用（后台重建？），本次检索直接使用当前索引")
            return self.store
        try:
            now = time.monotonic()
            if now - self._last_ensured_at < ttl:
                return self.store
            store = self.ensure_index()
            self._last_ensured_at = time.monotonic()
            return store
        finally:
            self._index_lock.release()


    def _apply_incremental(
        self,
        store: OpenSearchStore,
        file_fps: dict[str, str],
        old_fps: dict[str, str],
        added: list[str],
        changed: list[str],
        removed: list[str],
    ) -> None:
        """按文件粒度应用差异：先逐文件解析（失败隔离），成功项再删旧块并写入。

        单个坏文件不能拖垮整库检索：解析/切分失败的条目保留旧指纹（下次
        增量重试），其余文件照常更新。删除旧块放在新内容准备好之后，避免
        "先删后失败"把旧内容也弄丢。
        """
        parents = self._load_parents()
        if parents is None:
            logger.warning("parent 副本账本缺失，降级为全量重建")
            self._full_rebuild(store, file_fps)
            return

        old_manifest = self._read_manifest() or {}
        old_reports = dict(old_manifest.get("parse_reports") or {})
        parse_reports = dict(old_reports)

        loaded: list[tuple[str, list, list]] = []
        failures: dict[str, str] = {}
        file_reports: dict = {}
        for rel in added + changed:
            try:
                docs = load_documents(
                    self.settings.data_dir,
                    {rel},
                    **self._loader_kwargs(report=file_reports),
                )
                if not docs:
                    raise ValueError("未解析出内容")
                new_parents, new_children = self._split_parent_child(docs)
                loaded.append((rel, new_parents, new_children))
            except Exception as exc:
                failures[rel] = str(exc)[:200]
                logger.warning("增量索引跳过失败文件 %s：%s", rel, exc)

        # 删除旧块：全量删除项 + 已成功解析的修改项（新增项没有旧块）
        replaced = {rel for rel, _, _ in loaded if rel in changed}
        for rel in removed + sorted(replaced):
            deleted = store.delete_by_document(rel)
            if deleted:
                logger.info("已删除文件 %s 的 %d 个旧块", rel, deleted)
            else:
                logger.warning(
                    "删除 %s 命中 0 块（若为旧版 schema 索引，请手动全量重建）", rel
                )
        dropped = set(removed) | replaced
        if dropped:
            parents = [
                p
                for p in parents
                if str(p.metadata.get("relative_path") or "") not in dropped
            ]

        indexed = 0
        for rel, new_parents, new_children in loaded:
            if not new_children:
                parents.extend(new_parents)
                continue
            try:
                store.bulk_index(
                    new_children,
                    self.embeddings,
                    batch_size=self.settings.embed_batch_size,
                    batch_guard=self.gpu_guard,
                )
                parents.extend(new_parents)
                indexed += len(new_children)
            except Exception as exc:
                failures[rel] = str(exc)[:200]
                logger.warning("增量索引写入失败 %s：%s", rel, exc)

        # 失败文件保留旧指纹（下次继续重试）；新增但失败的文件不记账。
        effective_fps = dict(file_fps)
        for rel in failures:
            if rel in old_fps:
                effective_fps[rel] = old_fps[rel]
            else:
                effective_fps.pop(rel, None)
        # 解析报告：成功项覆盖；失败项保留旧报告；删除项移除
        for rel, report in file_reports.items():
            if rel not in failures:
                parse_reports[rel] = report
        for rel in failures:
            if rel in old_reports:
                parse_reports[rel] = old_reports[rel]
            else:
                parse_reports.pop(rel, None)
        for rel in removed:
            parse_reports.pop(rel, None)
        logger.info(
            "增量索引：%d 个文件成功（%d 块），%d 个失败待重试",
            len(loaded) - len(failures),
            indexed,
            len(failures),
        )
        self._save_meta(
            effective_fps,
            parents,
            child_count=store.doc_count(),
            parse_reports=parse_reports,
            # 增量只重解析了部分文件，必须沿用账本里的旧版本：写当前版本会让
            # index_health 误判"解析器已升级完成"，整库停在混合版本
            parse_version=old_manifest.get("parse_version") or "",
        )


    @staticmethod
    def _parsed_fingerprints(
        file_fps: dict[str, str], parse_reports: dict
    ) -> dict[str, str]:
        """只给解析成功的文件记账。

        失败文件若也写入指纹，下一次 ensure_index 会指纹命中直接复用，文件
        永久缺席索引；不记账则它会以 added 身份在下次增量里自动重试。
        """
        return {
            rel: fp
            for rel, fp in file_fps.items()
            if (parse_reports.get(rel) or {}).get("ok", True)
        }


    def _full_rebuild(
        self,
        store: OpenSearchStore,
        file_fps: dict[str, str],
        use_alias: bool = True,
    ) -> None:
        """全量重建。别名模式 = 蓝绿：写新物理索引 -> 原子切别名 -> 清理旧索引。"""
        parse_reports: dict = {}
        files_total = len(list_data_files(self.settings.data_dir))
        if self._rebuild_state.get("running"):
            self._rebuild_state["progress"] = {
                "phase": "parsing",
                "files_done": 0,
                "files_total": files_total,
                "pages_done": 0,
                "current": "",
            }

        def _progress(rel_path: str, pages: int) -> None:
            if not self._rebuild_state.get("running"):
                return
            p = self._rebuild_state.setdefault("progress", {})
            p["files_done"] = int(p.get("files_done") or 0) + 1
            p["pages_done"] = int(p.get("pages_done") or 0) + int(pages)
            p["current"] = rel_path

        documents = load_documents(
            self.settings.data_dir,
            **self._loader_kwargs(report=parse_reports, progress_cb=_progress),
        )
        if not documents:
            raise RuntimeError(
                f"数据目录 {self.settings.data_dir} 没有可索引的文档，请先上传文件"
            )
        parsed_fps = self._parsed_fingerprints(file_fps, parse_reports)
        if len(parsed_fps) != len(file_fps):
            logger.warning(
                "全量重建：%d 个文件解析失败，指纹不记账（下次增量自动重试）",
                len(file_fps) - len(parsed_fps),
            )
        parents, children = self._split_parent_child(documents)
        if self._rebuild_state.get("running"):
            self._rebuild_state.setdefault("progress", {})["phase"] = "embedding"
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
                    batch_guard=self.gpu_guard,
                )
                store.swap_alias(physical, alias=self.settings.opensearch_index)
            except Exception:
                # 失败清理新索引，别名仍指向旧索引（不影响检索）
                store.delete_index(physical)
                raise
            self._save_meta(
                parsed_fps,
                parents,
                child_count=len(children),
                parse_reports=parse_reports,
            )
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
                batch_guard=self.gpu_guard,
            )
            self._save_meta(
                parsed_fps,
                parents,
                child_count=len(children),
                parse_reports=parse_reports,
            )


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
