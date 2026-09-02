"""混合检索融合逻辑单测：跨查询 RRF + source 保底分流。"""

from __future__ import annotations

from langchain_core.documents import Document

from app.rag.retriever import merge_query_results


def _doc(content: str, source: str) -> Document:
    return Document(page_content=content, metadata={"source": source})


def _ranked(sources: list[str]) -> list[Document]:
    """构造一个"查询结果"：按给定 source 顺序排列（模拟每路检索的排名）。"""
    return [_doc(f"content-{i}-{src}", src) for i, src in enumerate(sources)]


def test_single_source_preserves_rrf_order():
    """单一来源：分流退化为纯 RRF 序，结果就是排名前 N 的块。"""
    docs = merge_query_results([_ranked(["book_a"] * 10)], limit=5)
    assert [d.page_content for d in docs] == [f"content-{i}-book_a" for i in range(5)]
    # 分数是 RRF 融合分（单调递减）
    scores = [d.metadata["query_merge_score"] for d in docs]
    assert scores == sorted(scores, reverse=True)


def test_multi_source_round_robin_guarantees_each_source():
    """多来源：轮流取候选，每个来源必进候选池（双格式垄断被打破）。"""
    # 模拟：同书双格式 pdf 20 个高排位、epub 3 个低排位（双格式竞争场景）
    ranked = _ranked(["pdf"] * 20 + ["epub"] * 3)
    docs = merge_query_results([ranked], limit=8)
    sources = [d.metadata["source"] for d in docs]
    assert sources == ["pdf", "epub", "pdf", "epub", "pdf", "epub", "pdf", "pdf"]
    # epub 的 3 个候选全部保留（轮流保证小源不丢失）
    epub_contents = [d.page_content for d in docs if d.metadata["source"] == "epub"]
    assert len(epub_contents) == 3


def test_fusion_score_preserved_across_queries():
    """多查询同内容：分数叠加后仍保留，分流不影响分数计算。"""
    q1 = _ranked(["book_a"] * 5)
    q2 = [_doc("content-0-book_a", "book_a"), _doc("dup-x", "book_b")]
    docs = merge_query_results([q1, q2], limit=5)
    dup = [d for d in docs if d.page_content == "content-0-book_a"]
    assert len(dup) == 1  # 跨查询按内容去重
    # 被两个查询命中的块分数 = 两个 1/(60+rank) 之和，高于单查询命中
    single = [d for d in docs if d.page_content == "content-1-book_a"][0]
    assert dup[0].metadata["query_merge_score"] > single.metadata["query_merge_score"]
