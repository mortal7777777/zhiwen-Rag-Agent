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
    BOOK_SCOPED_TOP_K,
    BROAD_CONTEXT_BUDGET,
    FRONT_MATTER_RERANK_PENALTY,
    TITLE_RERANK_BONUS,
    TOC_RERANK_PENALTY,
    book_scoped_children,
    ensure_parents,
    extract_titles,
    group_book_paths,
    hybrid_search,
    is_broad_question,
    is_enumeration_question,
    is_front_matter_like,
    is_meta_question,
    is_toc_like,
    merge_query_results,
    merge_scoped_children,
    select_contexts_with_book_quota,
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
        titles = extract_titles(question)
        queries = [question]
        by_book: dict[str, list[str]] = {}
        if use_expansion:
            start = time.perf_counter()
            try:
                queries, by_book = self.query_expander.expand_with_books(
                    question, history, titles
                )
            except Exception as exc:
                logger.warning("查询扩展失败，退化为原问题：%s", exc)
                queries = [question]
                by_book = {}
            metrics.record("expansion", time.perf_counter() - start)
            logger.info("查询扩展（%d 条）：%s", len(queries), queries)
            if by_book:
                logger.info("书内查询：%s", by_book)

        start = time.perf_counter()
        broad = is_broad_question(question)
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
        candidates = merge_query_results(
            per_query,
            limit=40,
            boost_titles=titles,
            original_weight=self.settings.query_original_weight,
        )
        # S3′ 书内检索：点名书在来源过滤下再检一次，书内 top child 直接进池
        scoped_children: list[Document] = []
        scoped_guarantees: list[Document] = []
        scoped_guarantee_keys: set[str] = set()
        if titles:
            try:
                rel_paths = [
                    f.relative_to(self.settings.data_dir).as_posix()
                    for f in list_data_files(self.settings.data_dir)
                ]
                # S3′：按逻辑书分组，用原问题 + per-book 查询 + 全局扩展
                # 分别做书内检索；每组按 parent 去重后取 top_k。per-book
                # 查询用一次批量 LLM 调用生成，专门补“问题词 vs 原句词”的
                # 错位（l2-kb-031/033/034、l1-kb-023 类）。
                vector_by_query = {q: v for q, v in pairs}
                for group in group_book_paths(titles, rel_paths):
                    group_titles = {
                        t
                        for rel in group
                        for t in titles
                        if title_matches_source(t, rel)
                    }
                    book_queries = {
                        query
                        for title in group_titles
                        for query in by_book.get(title, [])
                    }
                    query_list: list[str] = [question]
                    for title in group_titles:
                        for query in by_book.get(title, []):
                            if query not in query_list:
                                query_list.append(query)
                    for query in queries[1:]:
                        if query not in query_list:
                            query_list.append(query)
                    query_list = query_list[:6]
                    per_query: list[list[Document]] = []
                    for query in query_list:
                        vector = vector_by_query.get(query)
                        for rel in group:
                            docs = book_scoped_children(
                                self.store,
                                self.embeddings,
                                query,
                                rel,
                                top_k=BOOK_SCOPED_TOP_K,
                                demote_toc=demote_toc,
                                query_vector=vector,
                            )
                            per_query.append(docs)
                            # per-book 查询的 top parent 保底：多个查询的
                            # RRF 相加会让“序言块”（每条查询都命中）压过
                            # 只在一条 per-book 查询里排第一的正文块。
                            # 短查询和 HyDE 的 RRF 分数常打平，所以不能
                            # 只取“最高分一个”，每条 per-book 查询都保底
                            # （每本最多 2 条，总数最多 4 条）。
                            if query in book_queries and docs:
                                tops = small_to_big(
                                    docs[:1], 1, demote_toc=demote_toc
                                )
                                for top in tops:
                                    top_key = str(
                                        top.metadata.get("parent_id")
                                        or top.metadata.get("_id")
                                        or (top.page_content or "")[:64]
                                    )
                                    if (
                                        top_key not in scoped_guarantee_keys
                                        and len(scoped_guarantees) < 4
                                    ):
                                        scoped_guarantee_keys.add(top_key)
                                        scoped_guarantees.append(top)
                    scoped_children.extend(
                        merge_scoped_children(
                            per_query, top_k=BOOK_SCOPED_TOP_K
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
            # S6 查询级公平：多话题或宽泛题（主要观点/异同/如何写成等）时，
            # 每个扩展查询的 top parent 保底进池——避免单一篇章垄断精排名额；
            # per-book 查询的 top parent 也必须保底（见 S3′）。
            guaranteed: list[Document] = []
            if len(titles) >= 2 or broad:
                try:
                    for one_query in per_query:
                        tops = small_to_big(
                            one_query, 1, demote_toc=demote_toc
                        )
                        guaranteed.extend(
                            t
                            for t in tops
                            if not (demote_toc and is_toc_like(t.page_content))
                        )
                except Exception as exc:
                    logger.warning("query 级保底失败，跳过：%s", exc)
            guaranteed.extend(scoped_guarantees)
            if guaranteed:
                candidates = ensure_parents(
                    candidates, guaranteed, self.settings.max_parents
                )
        ranked = self.reranker.rerank(question, candidates, top_k=len(candidates))

        # S1 收尾 / S2 出口：点名书加分、目录块扣分；P2-lite 序言块在内容题
        # 上轻降权（版本/作者/写作过程题豁免），再截断。
        meta_question = is_meta_question(question)

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
            front_penalty = (
                FRONT_MATTER_RERANK_PENALTY
                if not meta_question and is_front_matter_like(doc.page_content)
                else 0.0
            )
            return (
                float(doc.metadata.get("rerank_score") or 0.0)
                + bonus
                - penalty
                - front_penalty
            )

        ranked.sort(key=_adjusted, reverse=True)
        # per-book 查询的最高分 top parent 直接占最终名额：只进精排池还不够，
        # 序言块仍可能把它挤出 top-k（l1-kb-023）。
        if scoped_guarantees:
            def _key(doc: Document) -> str:
                return str(
                    doc.metadata.get("parent_id")
                    or doc.metadata.get("_id")
                    or (doc.page_content or "")[:64]
                )

            forced_keys = {_key(doc) for doc in scoped_guarantees}
            forced = [doc for doc in ranked if _key(doc) in forced_keys]
            if forced:
                ranked = forced + [
                    doc for doc in ranked if _key(doc) not in forced_keys
                ]
        final_k = self.settings.rerank_top_k
        if broad or len(titles) >= 2 or by_book:
            final_k = max(final_k, BROAD_CONTEXT_BUDGET)
        docs = select_contexts_with_book_quota(ranked, titles, final_k)
        metrics.record("retrieval", time.perf_counter() - start)
        return docs
