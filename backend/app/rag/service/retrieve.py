"""RAG 服务 · 在线检索（从 rag/service.py 拆出，2026-10-10）。

检索管道编排（S1/S2/S3′/S5/S6 增强都在 retriever.py，本文件只做装配）。
注意：改检索行为需把 app/rag/retriever.py 的 RETRIEVAL_VERSION += 1。"""


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

class RetrieveMixin:
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
        # S2 目录块降权：枚举类问题（哪些/哪几/列举）豁免——清单式篇目就是答案
        demote_toc = not is_enumeration_question(question)
        # 提速：扩展查询一次性批量编码（省 N-1 次模型调用）；编码全部成功时
        # kNN/BM25 并发执行（OpenSearch 请求是 IO，httpx client 线程安全）。
        # 编码失败退回逐路串行编码（GPU 模型不允许并发 encode）。
        try:
            batch = list(self.embeddings.embed_queries(list(queries)))
        except Exception as exc:
            logger.warning("批量查询编码失败，退回逐路编码：%s", exc)
            batch = []
        vectors: list[list[float] | None] = (
            batch if len(batch) == len(queries) else [None] * len(queries)
        )

        def _search_one(pair: tuple[str, list[float] | None]) -> list[Document]:
            query, vector = pair
            return hybrid_search(
                store,
                self.embeddings,
                query,
                recall_k=self.settings.recall_k,
                candidate_pool=self.settings.candidate_pool,
                demote_toc=demote_toc,
                query_vector=vector,
            )

        pairs = list(zip(queries, vectors))
        if len(pairs) > 1 and all(v is not None for v in vectors):
            with ThreadPoolExecutor(max_workers=min(8, len(pairs))) as pool:
                per_query = list(pool.map(_search_one, pairs))
        else:
            per_query = [_search_one(p) for p in pairs]
        # S1 书名感知路由：问题里点名的书加权 + 保底（篇名匹配不到时自动退化）
        titles = extract_titles(question)
        candidates = merge_query_results(
            per_query,
            limit=40,
            boost_titles=titles,
            original_weight=self.settings.query_original_weight,
        )
        # S3′ 书内检索：点名书在来源过滤下再检一次，书内 top child 直接进池
        scoped_children: list[Document] = []
        if titles:
            try:
                rel_paths = [
                    f.relative_to(self.settings.data_dir).as_posix()
                    for f in list_data_files(self.settings.data_dir)
                ]
                # 多本书共享原问题向量（queries[0] 即原问题），免重复编码
                original_vector = vectors[0] if vectors else None
                for rel in match_book_paths(titles, rel_paths):
                    scoped_children.extend(
                        book_scoped_children(
                            self.store,
                            self.embeddings,
                            question,
                            rel,
                            demote_toc=demote_toc,
                            query_vector=original_vector,
                        )
                    )
            except Exception as exc:
                logger.warning("书内检索失败，跳过：%s", exc)
        if scoped_children:
            candidates = candidates + scoped_children
        if use_parent_child:
            candidates = small_to_big(
                candidates,
                self.settings.max_parents,
                boost_titles=titles,
                demote_toc=demote_toc,
            )
            # S6 查询级公平：多话题问题（≥2 个书名号）时，每个话题探针（扩展
            # 查询）的 top parent 保底进池——避免单一话题篇章垄断精排名额
            if len(titles) >= 2:
                try:
                    guaranteed: list[Document] = []
                    for one_query in per_query:
                        tops = small_to_big(
                            one_query, 1, demote_toc=demote_toc
                        )
                        guaranteed.extend(
                            t
                            for t in tops
                            if not (demote_toc and is_toc_like(t.page_content))
                        )
                    candidates = ensure_parents(
                        candidates, guaranteed, self.settings.max_parents
                    )
                except Exception as exc:
                    logger.warning("query 级保底失败，跳过：%s", exc)
        ranked = self.reranker.rerank(question, candidates, top_k=len(candidates))

        # S1 收尾 / S2 出口：点名书来源加精排分，目录块扣精排分（枚举题豁免），再截断
        def _adjusted(doc: Document) -> float:
            src = str(doc.metadata.get("source") or "")
            bonus = (
                TITLE_RERANK_BONUS
                if any(title_matches_source(t, src) for t in titles)
                else 0.0
            )
            penalty = (
                TOC_RERANK_PENALTY
                if demote_toc and is_toc_like(doc.page_content)
                else 0.0
            )
            return float(doc.metadata.get("rerank_score") or 0.0) + bonus - penalty

        ranked.sort(key=_adjusted, reverse=True)
        docs = ranked[: self.settings.rerank_top_k]
        metrics.record("retrieval", time.perf_counter() - start)
        return docs
