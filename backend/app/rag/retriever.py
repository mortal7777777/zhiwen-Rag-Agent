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
RETRIEVAL_VERSION = 7

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
# 书内检索（S3′）：每本逻辑书取 top-k child；最终选择每书至少 2 条、
# 单书最多占 k - 2*(n-1) 条，避免多书题被一本垄断。
BOOK_SCOPED_TOP_K = 6
BOOK_QUOTA_MIN_PER_BOOK = 2
BOOK_QUOTA_MAX_FOREIGN = 1
BROAD_CONTEXT_BUDGET = 8
# 序言/前言块在内容型问题里常抢走正文名额（l1-kb-023），但版本/作者/
# 写作过程类问题必须保留它们，所以只在非 meta 问题上轻惩罚。
FRONT_MATTER_RERANK_PENALTY = 0.03
_TOC_MARK_RE = re.compile(r"第[一二三四五六七八九十百零0-9]+[章节篇卷]")
_ENUM_KEYWORDS = (
    "哪些", "哪几", "列举", "包括",
    "分别收录", "收录了", "收录内容", "收录哪些",
    "目录", "篇目", "附录", "清单",
    "有哪些内容", "有什么内容", "包含哪些", "列一下",
)


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


# 中文书名与源文件名不一致的确定性别名。S1/S3′/最终配额都只按文件名
# 匹配，若不补这层，《造物的七个奥秘》匹配不到 The Seven Mysteries，
# 毛选篇名也匹配不到《毛泽东选集》容器文件。
_TITLE_ALIASES = {
    "造物的七个奥秘": ("The Seven Mysteries",),
    "实践论": ("毛泽东选集",),
    "矛盾论": ("毛泽东选集",),
    "湖南农民运动考察报告": ("毛泽东选集",),
    "论持久战": ("毛泽东选集",),
    "个人与群体事件的本质": ("群体实相",),
}


def title_matches_source(title: str, source: str) -> bool:
    """书名与来源文件名的归一化子串匹配（兼容下划线/括号/中文别名）。"""
    t = _norm_text(title)
    s = _norm_text(source)
    if not t:
        return False
    if t in s:
        return True
    return any(_norm_text(alias) in s for alias in _TITLE_ALIASES.get(t, ()))


_BROAD_KEYWORDS = (
    "主要观点", "主要讲", "主题", "核心", "如何写成", "异同", "分别",
    "关系", "概括", "讲了什么", "主要内容", "综述", "简要", "如何分析",
    "论述",
)
_META_KEYWORDS = (
    "版本", "序言", "前言", "作者", "译者", "出版", "时间跨度", "收录",
    "目录", "附录", "如何写成", "与谁", "写作过程",
)
_FRONT_MATTER_RE = re.compile(r"(序言|前言|译者序|导读|引言|出版说明|扉页)")


def is_broad_question(question: str) -> bool:
    return any(k in (question or "") for k in _BROAD_KEYWORDS)


def is_meta_question(question: str) -> bool:
    """版本/作者/写作过程类问题需要序言块，不能按内容题降权。"""
    return any(k in (question or "") for k in _META_KEYWORDS)


def is_front_matter_like(text: str) -> bool:
    return bool(text) and bool(_FRONT_MATTER_RE.search(text[:200]))


def match_book_paths(titles: list[str], relative_paths: list[str]) -> list[str]:
    """书名 → 库内文档相对路径（书名匹配文件名；篇名匹配不到返回空）。"""
    matched: list[str] = []
    for rel in relative_paths:
        name = rel.rsplit("/", 1)[-1]
        if any(title_matches_source(t, name) for t in titles):
            matched.append(rel)
    return matched


def group_book_paths(titles: list[str], relative_paths: list[str]) -> list[list[str]]:
    """把命中的库内路径按“逻辑书”分组。

    同一本书可能有上/下卷或多个格式；问题中的多个书名也可能落在同一本文集
    里（如两个篇名同属一本选集）。共享书名匹配的路径应视为同一本书，后续
    书内检索按组取 top_k，避免同一本书的多个文件挤占其他书。
    """
    matched = [
        rel
        for rel in relative_paths
        if any(title_matches_source(t, rel) for t in titles)
    ]
    if not matched:
        return []
    path_titles = {
        rel: {t for t in titles if title_matches_source(t, rel)}
        for rel in matched
    }
    parent = {rel: rel for rel in matched}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(matched):
        for b in matched[i + 1:]:
            if path_titles[a] & path_titles[b]:
                union(a, b)
    groups: dict[str, list[str]] = {}
    for rel in matched:
        groups.setdefault(find(rel), []).append(rel)
    return list(groups.values())


def book_scoped_children(
    store: OpenSearchStore,
    embeddings: LocalBGEEmbeddings,
    question: str,
    rel_path: str,
    top_k: int = 3,
    per_leg: int = 100,
    demote_toc: bool = True,
    query_vector: list[float] | None = None,
) -> list[Document]:
    """S3′ 书内检索：在点名书的来源过滤下跑 kNN+BM25 本地 RRF，取书内 top_k child。

    全局检索里点名书的正文块会被"引用块（他书提及书名）/同主题书/目录块"
    挤出（书名信号不对称），书内检索没有这些竞争，相关正文能直接浮现。
    多本书共享同一问题向量：传入 query_vector 时跳过重复编码。"""
    if query_vector is None:
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


def merge_scoped_children(
    per_query_docs: list[list[Document]],
    top_k: int = BOOK_SCOPED_TOP_K,
) -> list[Document]:
    """合并同一本逻辑书、多条查询的书内检索结果。

    每条查询内部已做 kNN+BM25 RRF；这里按 parent 去重后把跨查询分数相加，
    避免同一 parent 的多个 child 占满名额。返回值带 `query_merge_score`
    （与全局候选同一量级），供 small_to_big 与最终精排使用。
    """
    merged: dict[str, dict] = {}
    for docs in per_query_docs:
        for doc in docs:
            key = str(
                doc.metadata.get("parent_id")
                or doc.metadata.get("_id")
                or (doc.page_content or "")[:64]
            )
            score = float(doc.metadata.get("hybrid_score") or 0.0)
            item = merged.get(key)
            if item is None:
                merged[key] = {"doc": doc, "score": score, "best": score}
            else:
                item["score"] += score
                if score > item["best"]:
                    item["best"] = score
                    item["doc"] = doc
    ranked = sorted(merged.values(), key=lambda x: x["score"], reverse=True)[:top_k]
    result: list[Document] = []
    for item in ranked:
        doc = item["doc"]
        doc.metadata["query_merge_score"] = round(
            item["score"] * TITLE_BOOST, 4
        )
        result.append(doc)
    return result


def hybrid_search(
    store: OpenSearchStore,
    embeddings: LocalBGEEmbeddings,
    question: str,
    recall_k: int = 40,
    candidate_pool: int = 24,
    demote_toc: bool = True,
    query_vector: list[float] | None = None,
) -> list[Document]:
    """两路检索融合：
    - 稠密检索：kNN 向量相似度，擅长语义；
    - 稀疏检索：BM25 关键词，擅长精确词/专有名词；
    - RRF：融合分 = Σ 1/(K + rank)，两路都靠前的文本块分数更高。

    传入 query_vector 时跳过内部编码（批量编码 / 并行检索场景复用）。
    """
    if query_vector is None:
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


def select_contexts_with_book_quota(
    ranked: list[Document],
    titles: list[str],
    k: int,
    min_per_book: int = BOOK_QUOTA_MIN_PER_BOOK,
    max_foreign: int = BOOK_QUOTA_MAX_FOREIGN,
) -> list[Document]:
    """最终 top-k 选择：点名书目优先，多书题按书保底/限流。

    - 无书名：保持纯精排序；
    - 单书：点名书优先，外书最多 max_foreign 条（仅在点名书不足 k 时补位）；
    - 多书：每个逻辑书至少 min_per_book 条（候选足够时），单书最多
      max(min_per_book, k - min_per_book*(n-1))，防止一本垄断 top-k。
    """
    if not titles or k <= 0:
        return ranked[:k]

    # 书名 -> 实际命中的 source 集合；共享 source 的书名合并成一个逻辑书
    # （同一本书的上/下卷、多个格式，或同属一本选集的多个篇名）。
    title_sources: dict[str, set[str]] = {}
    for doc in ranked:
        src = str(doc.metadata.get("source") or "")
        for title in titles:
            if title_matches_source(title, src):
                title_sources.setdefault(title, set()).add(src)
    groups: list[dict] = []
    for title, srcs in title_sources.items():
        merged = False
        for group in groups:
            if group["sources"] & srcs:
                group["titles"].add(title)
                group["sources"] |= srcs
                merged = True
                break
        if not merged:
            groups.append({"titles": {title}, "sources": set(srcs)})
    if not groups:
        return ranked[:k]

    def group_of(doc: Document) -> int | None:
        src = str(doc.metadata.get("source") or "")
        for gi, group in enumerate(groups):
            if src in group["sources"]:
                return gi
        return None

    def key(doc: Document) -> str:
        return str(
            doc.metadata.get("parent_id")
            or doc.metadata.get("_id")
            or id(doc)
        )

    selected: list[Document] = []
    seen: set[str] = set()

    def add(doc: Document) -> bool:
        doc_key = key(doc)
        if doc_key in seen or len(selected) >= k:
            return False
        seen.add(doc_key)
        selected.append(doc)
        return True

    if len(groups) == 1:
        for doc in ranked:
            if group_of(doc) == 0:
                add(doc)
        # 点名书候选不足 k 时，外书最多补 max_foreign 条
        foreign_added = 0
        for doc in ranked:
            if foreign_added >= max_foreign or len(selected) >= k:
                break
            if group_of(doc) is None and add(doc):
                foreign_added += 1
    else:
        min_per_book = max(1, min(min_per_book, k // len(groups)))
        max_per_book = max(
            min_per_book, k - min_per_book * (len(groups) - 1)
        )
        counts = [0] * len(groups)
        # 每书保底
        for gi in range(len(groups)):
            for doc in ranked:
                if counts[gi] >= min_per_book:
                    break
                if group_of(doc) == gi and add(doc):
                    counts[gi] += 1
        # 按精排补位，单书不超过 max_per_book
        for doc in ranked:
            if len(selected) >= k:
                break
            gi = group_of(doc)
            if gi is None or counts[gi] >= max_per_book:
                continue
            if add(doc):
                counts[gi] += 1
        # 仍有空位时，用外书补足（只在点名书候选不足时发生）
        for doc in ranked:
            if len(selected) >= k:
                break
            if group_of(doc) is None:
                add(doc)

    order = {key(doc): i for i, doc in enumerate(ranked)}
    if len(groups) == 1:
        # 单书题：点名书排在外书之前，组内仍按精排序
        selected.sort(
            key=lambda doc: (
                group_of(doc) is None,
                order.get(key(doc), len(ranked)),
            )
        )
    else:
        selected.sort(key=lambda doc: order.get(key(doc), len(ranked)))
    return selected[:k]


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
