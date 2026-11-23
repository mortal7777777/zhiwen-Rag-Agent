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
    SCOPE_GUARANTEE_MIN_SCORE,
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
    set_dynamic_title_aliases,
    small_to_big,
    title_in_text,
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
    @staticmethod
    def _resolve_title_sources(
        store: OpenSearchStore,
        titles: list[str],
        rel_paths: list[str],
    ) -> dict[str, tuple[str, ...]]:
        """查询期解析“篇名 → 合集文件”：文件名匹配不到时用 BM25 找来源。

        只接受最高分命中里确实出现该篇名的来源（title_in_text 证据校验），
        避免把正文顺带提及的其他书名误当成篇名路由；找不到则保持原行为。
        """
        resolved: dict[str, tuple[str, ...]] = {}
        for title in titles:
            if any(title_matches_source(title, rel) for rel in rel_paths):
                continue
            try:
                hits = store.search_bm25(title, 5)
            except Exception as exc:
                logger.warning("篇名解析检索失败（%s）：%s", title, exc)
                continue
            for hit in hits:
                rel = str(hit.metadata.get("relative_path") or "")
                if rel and title_in_text(title, hit.page_content):
                    resolved[title] = (rel.rsplit("/", 1)[-1],)
                    break
        if resolved:
            logger.info("篇名路由（查询期解析）：%s", resolved)
        return resolved


    def retrieve(
        self,
        question: str,
        history: list[dict] | None = None,
        rewrite: bool | None = None,
        parent_child: bool | None = None,
        scope_paths: list[str] | None = None,
    ) -> list[Document]:
        """检索流水线：查询扩展 -> 混合检索 -> 跨查询 RRF -> Parent 聚合 -> 精排。

        - rewrite：是否启用查询扩展（Multi-Query/HyDE/多轮补全，默认取配置）；
        - parent_child：是否启用 Parent-Child（默认取配置）；
        - scope_paths：限定知识库检索范围（单文档/整本书的多个相对路径）。
        """
        store = self.ensure_index_cached()
        use_parent_child = (
            self.settings.parent_child_enabled if parent_child is None else parent_child
        )

        use_expansion = (
            self.settings.query_expansion_enabled if rewrite is None else rewrite
        )
        # 每次检索重置请求级篇名别名，避免线程复用/异常路径留下上一轮状态。
        set_dynamic_title_aliases(None)
        titles = extract_titles(question)
        rel_paths: list[str] = []
        if titles:
            rel_paths = [
                f.relative_to(self.settings.data_dir).as_posix()
                for f in list_data_files(self.settings.data_dir)
            ]
            set_dynamic_title_aliases(
                self._resolve_title_sources(store, titles, rel_paths)
            )
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
        # 书内查询也并入同一次批量编码：by_book 的查询不在 queries 里，
        # 只编码 queries 会让 S3′ 对每条 per-book 查询重复单条 embed_query
        # （GPU 串行编码 + 多次 OpenSearch 往返）。
        extra_queries: list[str] = []
        for book_queries in by_book.values():
            for one in book_queries:
                if one not in queries and one not in extra_queries:
                    extra_queries.append(one)
        embed_targets = list(queries) + extra_queries
        # 提速：扩展查询一次性批量编码（省 N-1 次模型调用）；编码全部成功时
        # kNN/BM25 并发执行（OpenSearch 请求是 IO，httpx client 线程安全）。
        # 编码失败退回逐路串行编码（GPU 模型不允许并发 encode）。
        try:
            batch = list(self.embeddings.embed_queries(embed_targets))
        except Exception as exc:
            logger.warning("批量查询编码失败，退回逐路编码：%s", exc)
            batch = []
        if len(batch) == len(embed_targets):
            vectors: list[list[float] | None] = list(batch[: len(queries)])
            vector_by_query: dict[str, list[float] | None] = dict(
                zip(embed_targets, batch)
            )
        else:
            vectors = [None] * len(queries)
            vector_by_query = {}

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
                filter_rel_paths=scope_paths,
            )

        def _scoped_one(task: tuple[str, str]) -> list[Document]:
            query, rel = task
            return book_scoped_children(
                self.store,
                self.embeddings,
                query,
                rel,
                top_k=BOOK_SCOPED_TOP_K,
                demote_toc=demote_toc,
                query_vector=vector_by_query.get(query),
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
        # 用户已显式限定范围时，全局检索已按 scope_paths 过滤；S3′ 的书名
        # 路由不再需要，且可能把范围外书籍的块带进候选池。
        if titles and not scope_paths:
            try:
                # S3′：按逻辑书分组，用原问题 + per-book 查询 + 全局扩展
                # 分别做书内检索；每组按 parent 去重后取 top_k。per-book
                # 查询用一次批量 LLM 调用生成，专门补“问题词 vs 原句词”的
                # 错位（l2-kb-031/033/034、l1-kb-023 类）。
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
                    tasks = [
                        (query, rel) for query in query_list for rel in group
                    ]
                    # 向量已批量备齐时，书内 kNN/BM25 都是 IO，可并发执行；
                    # 编码失败（vector 缺失）则退回串行，避免 GPU 并发 encode。
                    if len(tasks) > 1 and all(
                        vector_by_query.get(query) is not None
                        for query, _ in tasks
                    ):
                        with ThreadPoolExecutor(
                            max_workers=min(8, len(tasks))
                        ) as pool:
                            flat = list(pool.map(_scoped_one, tasks))
                    else:
                        flat = [_scoped_one(task) for task in tasks]
                    per_query: list[list[Document]] = []
                    pos = 0
                    for query in query_list:
                        for _rel in group:
                            docs = flat[pos]
                            pos += 1
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
            # 用原始 rerank_score 判定（不能用 _adjusted：点名书自带
            # TITLE_RERANK_BONUS，会把无关块的调整分抬过门槛）。
            forced = [
                doc
                for doc in ranked
                if _key(doc) in forced_keys
                and float(doc.metadata.get("rerank_score") or 0.0)
                >= SCOPE_GUARANTEE_MIN_SCORE
            ]
            if forced:
                promoted_keys = {_key(doc) for doc in forced}
                ranked = forced + [
                    doc for doc in ranked if _key(doc) not in promoted_keys
                ]
        final_k = self.settings.rerank_top_k
        if broad or len(titles) >= 2 or by_book:
            final_k = max(final_k, BROAD_CONTEXT_BUDGET)
        docs = select_contexts_with_book_quota(ranked, titles, final_k)
        metrics.record("retrieval", time.perf_counter() - start)
        set_dynamic_title_aliases(None)
        return docs
