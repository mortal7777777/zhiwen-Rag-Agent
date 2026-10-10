"""RAG 服务 · 文档与生成（从 rag/service.py 拆出，2026-10-10）。

ask（检索+生成）、文档增删列表、重建索引与状态。"""


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

from .base import UploadOutcome

class DocsMixin:
    def ask(self, question: str, history: list[dict] | None = None) -> dict:
        """完整 RAG 流程：检索 -> 精排 -> 生成，返回回答与引用来源。"""
        metrics.inc("chat_requests")
        try:
            docs = self.retrieve_locked(question, history=history)
            # S5 相关度门槛：库内没有相关内容（top1 低于阈值）时不喂生成模型，
            # 避免随机块被当依据；0=关闭。校准：库外题 top1≤0.01，库内题≥0.54。
            min_score = float(effective(self.settings, "kb_chat_min_score") or 0)
            best = max(
                (
                    float(
                        d.metadata.get("rerank_score")
                        or d.metadata.get("parent_score")
                        or 0
                    )
                    for d in docs
                ),
                default=0.0,
            )
            if docs and min_score > 0 and best < min_score:
                metrics.inc("chat_no_context")
                return {
                    "answer": "知识库中没有找到与这个问题相关的内容。",
                    "sources": [],
                }
            context = "\n\n".join(doc.page_content for doc in docs)
            start = time.perf_counter()
            # 生成段不占 GPU 锁，但受 LLM 并发上限约束（与 Agent 路径一致）
            self.acquire_llm()
            try:
                answer = self.chat.generate(question, context, history)
            finally:
                self.release_llm()
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


    def add_documents(
        self,
        files: list[UploadFile],
        *,
        dry_run: bool = False,
        conflict_policy: str = "ask",
        archive_version_no: str | None = None,
        archive_note: str = "",
    ) -> UploadOutcome:
        """保存上传文件到 data/ 并增量更新索引（带查重与同名归档）。

        - dry_run：只做查重、不落盘；
        - conflict_policy="ask" 且检出冲突：返回 conflict（API 层转 409）；
        - conflict_policy="proceed"：仅警告档直接放行；同名档要求 archive_version_no，
          旧文件移到 data_versions/ 归档（archived 元数据交 API 层落库）。
        """
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        # 先读字节 + 校验（路径穿越/格式）
        items: list[tuple[str, bytes]] = []
        for file in files:
            filename = Path(file.filename or "未命名").name
            suffix = Path(filename).suffix.lower()
            if suffix not in SUPPORTED_SUFFIXES:
                supported = " / ".join(sorted(SUPPORTED_SUFFIXES))
                raise ValueError(
                    f"不支持的文件格式：{filename}（当前支持：{supported}）"
                )
            items.append((filename, file.file.read()))
        if not items:
            raise ValueError("没有可上传的文件")
        names = [name.lower() for name, _ in items]
        if len(set(names)) != len(names):
            raise ValueError("批量上传中包含同名文件，请逐个上传")

        with self.index_lock():
            # 冲突检测（锁内执行，消 TOCTOU）
            checks: list[dict] = []
            for filename, raw in items:
                conflicts = self.dedup.check(filename, raw)
                checks.append({"file_name": filename, "conflicts": conflicts})

            if dry_run:
                return UploadOutcome(mode="dry_run", conflicts=checks)

            same_name_checks = [
                c
                for c in checks
                if any(x["kind"] == "same_name" for x in c["conflicts"])
            ]
            has_any = any(c["conflicts"] for c in checks)
            if has_any and conflict_policy != "proceed":
                return UploadOutcome(mode="conflict", conflicts=checks)
            if len(same_name_checks) >= 2:
                raise ValueError("批量上传包含多个同名文件，请逐个上传")
            if same_name_checks and not (archive_version_no or "").strip():
                raise ValueError("同名文件重传需要指定归档版本号")

            archived: list[dict] = []
            for filename, raw in items:
                target = self.settings.data_dir / filename
                same_name = any(
                    x["kind"] == "same_name"
                    for c in checks
                    if c["file_name"] == filename
                    for x in c["conflicts"]
                )
                if same_name and target.exists():
                    version_no = validate_version_no(archive_version_no or "")
                    size = target.stat().st_size
                    path = archive_file(self.settings, filename, target, version_no)
                    archived.append(
                        {
                            "doc_relative_path": filename,
                            "version_no": version_no,
                            "file_name": path.name,
                            "original_name": filename,
                            "size": size,
                            "note": (archive_note or "").strip(),
                        }
                    )
                    logger.info("同名重传：旧文件已归档为 %s", path)
                target.write_bytes(raw)
                logger.info("已保存文件：%s", filename)
            self.ensure_index()
            return UploadOutcome(
                mode="committed",
                documents=self.list_documents(),
                archived=archived,
            )


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
