"""混合检索：OpenSearch kNN + BM25，RRF 融合；跨查询合并；Parent-Child 聚合。"""

from __future__ import annotations

from typing import Callable

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from .embeddings import LocalBGEEmbeddings
from .store import OpenSearchStore

RRF_K = 60  # RRF 平滑常数：排名越靠前，融合分越高


def hybrid_search(
    store: OpenSearchStore,
    embeddings: LocalBGEEmbeddings,
    question: str,
    recall_k: int = 40,
    candidate_pool: int = 24,
) -> list[Document]:
    """两路检索融合：
    - 稠密检索：kNN 向量相似度，擅长语义；
    - 稀疏检索：BM25 关键词，擅长精确词/专有名词；
    - RRF：融合分 = Σ 1/(K + rank)，两路都靠前的文本块分数更高。
    """
    query_vector = embeddings.embed_query(question)
    dense_docs = store.search_vector(query_vector, recall_k)
    sparse_docs = store.search_bm25(question, recall_k)

    merged: dict[str, dict] = {}
    for docs in (dense_docs, sparse_docs):
        for rank, doc in enumerate(docs, 1):
            item = merged.setdefault(doc.page_content, {"doc": doc, "rrf": 0.0})
            item["rrf"] += 1.0 / (RRF_K + rank)

    ranked = sorted(merged.values(), key=lambda item: item["rrf"], reverse=True)
    result: list[Document] = []
    for item in ranked[:candidate_pool]:
        doc = item["doc"]
        doc.metadata["hybrid_score"] = round(item["rrf"], 4)
        result.append(doc)
    return result


def merge_query_results(
    query_results: list[list[Document]],
    limit: int = 40,
) -> list[Document]:
    """把多个改写查询的检索结果做二次 RRF 合并。

    每个查询内部已经做过 kNN+BM25 的 RRF，这里按"查询内排名"再融合一次，
    让多个角度的查询都能贡献候选，同时抑制单个查询的噪声。

    2026-08 加 source 保底分流：融合后按来源轮流取候选，避免同一本书的
    多格式（如同一本书的 epub+pdf）垄断整个候选池，把多跳题第二篇文档的内容
    挤出候选（l2-kb-006 recall=0 根因之一）。
    """
    merged: dict[str, dict] = {}
    for docs in query_results:
        for rank, doc in enumerate(docs, 1):
            item = merged.setdefault(doc.page_content, {"doc": doc, "rrf": 0.0})
            item["rrf"] += 1.0 / (RRF_K + rank)
    ranked = sorted(merged.values(), key=lambda item: item["rrf"], reverse=True)

    # 按 source 分组后轮流取队首：单源题退化为纯 RRF 序，多源题保证每源必进
    from collections import deque

    buckets: dict[str, deque] = {}
    for item in ranked:
        src = item["doc"].metadata.get("source") or "?"
        buckets.setdefault(src, deque()).append(item)

    score_by_content = {item["doc"].page_content: item["rrf"] for item in ranked}
    result: list[Document] = []
    pending = deque(buckets.keys())
    while len(result) < limit and pending:
        src = pending.popleft()
        bucket = buckets[src]
        if bucket:
            doc = bucket.popleft()["doc"]
            doc.metadata["query_merge_score"] = round(
                score_by_content.get(doc.page_content, 0.0), 4
            )
            result.append(doc)
            if bucket:
                pending.append(src)
    return result


def small_to_big(
    child_docs: list[Document],
    max_parents: int = 6,
) -> list[Document]:
    """Parent-Child（小到大）：按 parent_id 聚合 child，返回 parent 全文作为上下文。

    - child 负责精确定位（召回粒度小）；
    - parent 负责上下文完整（语义不再被切块截断）；
    - 同一个 parent 只保留相关性最高的 child 的分数。
    """
    groups: dict[str, dict] = {}
    for doc in child_docs:
        parent_id = doc.metadata.get("parent_id") or doc.metadata.get("_id")
        score = doc.metadata.get("query_merge_score") or doc.metadata.get("hybrid_score") or 0.0
        # 旧索引没有 parent 字段时，直接按 child 内容本身去重
        key = parent_id or doc.page_content
        group = groups.setdefault(key, {"doc": doc, "score": 0.0})
        if score > group["score"]:
            group["doc"] = doc
            group["score"] = score

    ranked = sorted(groups.values(), key=lambda item: item["score"], reverse=True)
    parents: list[Document] = []
    for item in ranked[:max_parents]:
        child = item["doc"]
        parent_content = child.metadata.get("parent_content") or child.page_content
        metadata = dict(child.metadata)
        metadata["parent_score"] = round(item["score"], 4)
        parent_doc = Document(page_content=parent_content, metadata=metadata)
        parents.append(parent_doc)
    return parents


class KnowledgeBaseRetriever(BaseRetriever):
    """把现有 RAG 检索管道包装成 LangChain Retriever。

    通过注入 retrieve_fn 避免与 RAGService 形成循环依赖；
    LangChain Agent 用 invoke(query) 调用，拿到的是重排后的 Document 列表。
    """

    retrieve_fn: Callable[[str], list[Document]]

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager=None,
    ) -> list[Document]:
        return self.retrieve_fn(query)
