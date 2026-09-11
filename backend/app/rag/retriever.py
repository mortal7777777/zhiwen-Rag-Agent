"""混合检索：OpenSearch kNN + BM25，RRF 融合；跨查询合并；Parent-Child 聚合。"""

from __future__ import annotations

import re
from typing import Callable

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from .embeddings import LocalBGEEmbeddings
from .store import OpenSearchStore

RRF_K = 60  # RRF 平滑常数：排名越靠前，融合分越高

# 检索代码版本：改检索逻辑时 +1（eval_ragas 检索缓存指纹的一部分，
# 保证代码改动后评测不会命中旧检索缓存）。
RETRIEVAL_VERSION = 5

# ---- 书名感知路由（S1，2026-09-11）----
# 问题含《书名》时，命中该书的候选在跨查询融合中加权（TITLE_BOOST），且在
# parent 选择中保底入池（每本 TITLE_GUARANTEE_PER_BOOK 个）——纯语义精排对
# 同主题语料（同类文集之间、同作者系列书之间）分辨不出"哪本书"，实测 21 题
# 有 5 题的正确书目被压到第 2-6 位。篇名（不是文件名）匹配不到时自动退化为
# 原行为（零风险）。
TITLE_BOOST = 2.0
TITLE_GUARANTEE_PER_BOOK = 3
# 精排分加成：保底只保证"进池"，精排仍会把无关书排在前面（实测正确书目
# 常排 #4-6）；加成让点名书目优先占住最终上下文名额（0-1 分制，0.1 ≈ 明显提升）。
TITLE_RERANK_BONUS = 0.1

# ---- 目录/清单块治理（S2，2026-09-11）----
# 目录页/章节清单的关键词密度天然高（章节名+术语扎堆），BM25 和 cross-encoder
# 都给高分，但多数不承载答案内容（实测某题曾 4/6 输出槽被目录页占用）。
# 融合层降权（入口）+ 精排层轻惩罚（出口）。注意两个边界：
# 1) 降权要"轻"：目录/篇目有时是合法覆盖的来源（章节条目可能覆盖参考提法），
#    强降权会连合法覆盖一起砍掉；2) 枚举类问题（"哪些/哪几/列举"）豁免——
#    书名/篇目清单本身就是答案（某清单类书目的答案恰在其目录页里，
#    强降权实测命中 7/7→2/7）。
TOC_DEMOTE_RRF = 0.7
TOC_RERANK_PENALTY = 0.08
_TOC_MARK_RE = re.compile(r"第[一二三四五六七八九十百零0-9]+[章节篇卷]")
_ENUM_KEYWORDS = ("哪些", "哪几", "列举", "包括")


def is_toc_like(text: str) -> bool:
    """章节清单型目录页识别（启发式）：前 60 字含"目录/Table of Contents"，
    或"第X章"类标记 ≥4 次（正文段落一般 0-2 次，目录页 5+ 次）。"""
    if not text:
        return False
    if "Table of Contents" in text or re.search(r"目\s*录", text[:60]):
        return True
    return len(_TOC_MARK_RE.findall(text)) >= 4


def is_enumeration_question(question: str) -> bool:
    """枚举类问题（答案是清单）：这类问题的目录/篇目块可能是正解，不降权。"""
    return any(k in (question or "") for k in _ENUM_KEYWORDS)
_TITLE_STRIP = re.compile(r"[\s_（）()【】\[\]·・、，,。.]+")


def extract_titles(question: str) -> list[str]:
    """提取问题中的书名号内容（去重保序；长度限 40 防误配）。"""
    seen: set[str] = set()
    titles: list[str] = []
    for t in re.findall(r"《([^《》]{1,40})》", question or ""):
        if t not in seen:
            seen.add(t)
            titles.append(t)
    return titles


def _norm_text(text: str) -> str:
    return _TITLE_STRIP.sub("", str(text or "")).lower()


def title_matches_source(title: str, source: str) -> bool:
    """书名与来源文件名的归一化子串匹配（兼容"春_桥文录"这类下划线/括号差异）。"""
    t = _norm_text(title)
    return bool(t) and t in _norm_text(source)


def match_book_paths(titles: list[str], relative_paths: list[str]) -> list[str]:
    """书名 → 库内文档相对路径（书名匹配文件名；篇名匹配不到返回空）。"""
    matched: list[str] = []
    for rel in relative_paths:
        name = rel.rsplit("/", 1)[-1]
        if any(title_matches_source(t, name) for t in titles):
            matched.append(rel)
    return matched


def book_scoped_children(
    store: OpenSearchStore,
    embeddings: LocalBGEEmbeddings,
    question: str,
    rel_path: str,
    top_k: int = 3,
    per_leg: int = 100,
    demote_toc: bool = True,
) -> list[Document]:
    """S3′ 书内检索：在点名书的来源过滤下跑 kNN+BM25 本地 RRF，取书内 top_k child。

    全局检索里点名书的正文块会被"引用块（他书提及书名）/同主题书/目录块"
    挤出（书名信号不对称），书内检索没有这些竞争，相关正文能直接浮现。"""
    query_vector = embeddings.embed_query(question)
    dense = store.search_vector(query_vector, per_leg, filter_rel_path=rel_path)
    sparse = store.search_bm25(question, per_leg, filter_rel_path=rel_path)
    merged: dict[str, dict] = {}
    for docs in (dense, sparse):
        for rank, doc in enumerate(docs, 1):
            item = merged.setdefault(doc.page_content, {"doc": doc, "rrf": 0.0})
            item["rrf"] += 1.0 / (RRF_K + rank)
    ranked = sorted(merged.values(), key=lambda item: item["rrf"], reverse=True)
    # 书内也会出现本书自己的目录页（枚举类问题同样豁免）
    if demote_toc:
        demoted = False
        for item in ranked:
            if is_toc_like(item["doc"].page_content):
                item["rrf"] = round(item["rrf"] * TOC_DEMOTE_RRF, 6)
                demoted = True
        if demoted:
            ranked.sort(key=lambda item: item["rrf"], reverse=True)
    result: list[Document] = []
    for item in ranked[:top_k]:
        doc = item["doc"]
        doc.metadata["hybrid_score"] = round(item["rrf"], 4)
        result.append(doc)
    return result


def hybrid_search(
    store: OpenSearchStore,
    embeddings: LocalBGEEmbeddings,
    question: str,
    recall_k: int = 40,
    candidate_pool: int = 24,
    demote_toc: bool = True,
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
    # S2 目录块治理：章节清单型块在融合分上降权（候选竞争阶段就让位给正文块；
    # 枚举类问题由调用方传 demote_toc=False 豁免）
    if demote_toc:
        demoted = False
        for item in ranked:
            if is_toc_like(item["doc"].page_content):
                item["rrf"] = round(item["rrf"] * TOC_DEMOTE_RRF, 6)
                demoted = True
        if demoted:
            ranked.sort(key=lambda item: item["rrf"], reverse=True)
    result: list[Document] = []
    for item in ranked[:candidate_pool]:
        doc = item["doc"]
        doc.metadata["hybrid_score"] = round(item["rrf"], 4)
        result.append(doc)
    return result


def merge_query_results(
    query_results: list[list[Document]],
    limit: int = 40,
    boost_titles: list[str] | None = None,
    original_weight: float = 1.0,
) -> list[Document]:
    """把多个改写查询的检索结果做二次 RRF 合并。

    每个查询内部已经做过 kNN+BM25 的 RRF，这里按"查询内排名"再融合一次，
    让多个角度的查询都能贡献候选，同时抑制单个查询的噪声。

    2026-08 加 source 保底分流：融合后按来源轮流取候选，避免同一本书的
    多格式（如同一本书的 epub+pdf）垄断整个候选池，把多跳题第二篇文档的内容
    挤出候选（l2-kb-006 recall=0 根因之一）。
    2026-09-11 加两个旋钮：
    - boost_titles（S1）：问题里的书名，命中的来源融合分 ×TITLE_BOOST；
    - original_weight（S3 实验旋钮）：原问题（queries[0]）的贡献权重。
    """
    merged: dict[str, dict] = {}
    for qi, docs in enumerate(query_results):
        weight = original_weight if qi == 0 else 1.0
        for rank, doc in enumerate(docs, 1):
            item = merged.setdefault(doc.page_content, {"doc": doc, "rrf": 0.0})
            item["rrf"] += weight * 1.0 / (RRF_K + rank)
    ranked = sorted(merged.values(), key=lambda item: item["rrf"], reverse=True)
    if boost_titles:
        boosted = False
        for item in ranked:
            src = item["doc"].metadata.get("source") or ""
            if any(title_matches_source(t, src) for t in boost_titles):
                item["rrf"] = round(item["rrf"] * TITLE_BOOST, 6)
                boosted = True
        if boosted:
            ranked.sort(key=lambda item: item["rrf"], reverse=True)

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
    boost_titles: list[str] | None = None,
    demote_toc: bool = True,
) -> list[Document]:
    """Parent-Child（小到大）：按 parent_id 聚合 child，返回 parent 全文作为上下文。

    - child 负责精确定位（召回粒度小）；
    - parent 负责上下文完整（语义不再被切块截断）；
    - 同一个 parent 只保留相关性最高的 child 的分数；
    - boost_titles（S1）：问题里的书名，每本保底 TITLE_GUARANTEE_PER_BOOK 个
      parent 进入精排池（否则正确书目可能被同主题书整体挤出）。
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

    # S2：父块级目录识别（child 切片可能漏检，parent 全文更可靠）→ 分数降权
    if demote_toc:
        for g in groups.values():
            parent_text = g["doc"].metadata.get("parent_content") or g["doc"].page_content
            if is_toc_like(parent_text):
                g["score"] = round(g["score"] * TOC_DEMOTE_RRF, 6)
    ranked = sorted(groups.values(), key=lambda item: item["score"], reverse=True)
    if boost_titles:
        # 保底：命中书名的 parent 每本取分数最高的 N 个，前置进选择序
        guaranteed: list[dict] = []
        for t in boost_titles:
            matched = [
                g
                for g in ranked
                if title_matches_source(t, g["doc"].metadata.get("source") or "")
            ]
            guaranteed.extend(matched[:TITLE_GUARANTEE_PER_BOOK])
        ordered: list[dict] = []
        seen_keys: set[str] = set()
        for g in guaranteed + ranked:
            key = g["doc"].metadata.get("parent_id") or g["doc"].page_content
            if key in seen_keys:
                continue
            seen_keys.add(key)
            ordered.append(g)
            if len(ordered) >= max_parents:
                break
        ranked = ordered
    parents: list[Document] = []
    for item in ranked[:max_parents]:
        child = item["doc"]
        parent_content = child.metadata.get("parent_content") or child.page_content
        metadata = dict(child.metadata)
        metadata["parent_score"] = round(item["score"], 4)
        parent_doc = Document(page_content=parent_content, metadata=metadata)
        parents.append(parent_doc)
    return parents


def ensure_parents(
    pool: list[Document],
    guaranteed: list[Document],
    max_parents: int,
) -> list[Document]:
    """S6：把保底 parent 并入精排池（parent_id 去重；超限时挤掉分数最低项）。

    用于"查询级公平"：多话题问题的每个扩展查询（话题探针）各自的 top parent
    保底入池，避免单一话题的篇章垄断 10 个精排名额（l2-kb-006 根因）。"""
    def _key(doc: Document) -> str:
        return str(doc.metadata.get("parent_id") or doc.page_content[:64])

    have = {_key(d) for d in pool}
    add = [g for g in guaranteed if _key(g) not in have]
    if not add:
        return pool
    kept = list(pool)
    while len(kept) + len(add) > max_parents and kept:
        kept.sort(key=lambda d: float(d.metadata.get("parent_score") or 0.0))
        kept.pop(0)
    return kept + add


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
