"""混合检索融合逻辑单测：跨查询 RRF + source 保底分流。"""

from __future__ import annotations

from langchain_core.documents import Document

from app.rag.llm import DeepSeekChat
from app.rag.retriever import (
    group_book_paths,
    is_broad_question,
    is_enumeration_question,
    is_front_matter_like,
    is_meta_question,
    merge_query_results,
    merge_scoped_children,
    select_contexts_with_book_quota,
    title_matches_source,
)


def _doc(content: str, source: str) -> Document:
    return Document(page_content=content, metadata={"source": source})


def _meta_doc(content: str, source: str, parent_id: str, score: float = 0.0) -> Document:
    return Document(
        page_content=content,
        metadata={
            "source": source,
            "parent_id": parent_id,
            "hybrid_score": score,
        },
    )


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


def test_enumeration_intent_recognizes_content_lists():
    assert is_enumeration_question(
        "《王洪文文集》与《王洪文文革讲话集》分别收录了什么内容？"
    )
    assert is_enumeration_question("这本书的收录内容有哪些？")
    assert is_enumeration_question("目录和附录包括什么？")
    assert not is_enumeration_question("《狂喜的艺术》中静心与庆祝是什么关系？")


def test_group_book_paths_merges_volumes_and_shared_titles():
    groups = group_book_paths(
        ["个人实相", "群体实相"],
        ["个人实相(上).txt", "个人实相(下).txt", "群体实相.txt"],
    )
    assert sorted(len(group) for group in groups) == [1, 2]
    assert any(
        set(group) == {"个人实相(上).txt", "个人实相(下).txt"}
        for group in groups
    )


def test_group_book_paths_merges_titles_sharing_source():
    groups = group_book_paths(
        ["个人实相", "个人实相(上)"],
        ["个人实相(上).txt"],
    )
    assert groups == [["个人实相(上).txt"]]


def test_merge_scoped_children_dedupes_parent_and_sums_scores():
    q1 = [
        _meta_doc("p1-a", "book_a", "p1", 0.03),
        _meta_doc("p2-a", "book_a", "p2", 0.02),
    ]
    q2 = [_meta_doc("p1-b", "book_a", "p1", 0.01)]
    docs = merge_scoped_children([q1, q2], top_k=2)
    assert [d.metadata["parent_id"] for d in docs] == ["p1", "p2"]
    assert docs[0].metadata["query_merge_score"] > docs[1].metadata["query_merge_score"]
    assert docs[0].page_content == "p1-a"


def test_quota_single_book_prefers_named_then_one_foreign():
    ranked = [
        _meta_doc("A1", "book_a", "a1"),
        _meta_doc("C1", "book_c", "c1"),
        _meta_doc("A2", "book_a", "a2"),
        _meta_doc("A3", "book_a", "a3"),
        _meta_doc("C2", "book_c", "c2"),
    ]
    docs = select_contexts_with_book_quota(ranked, ["book_a"], k=6)
    assert [d.page_content for d in docs] == ["A1", "A2", "A3", "C1"]


def test_quota_multi_book_min_two_each_and_cap_four():
    ranked = [
        _meta_doc("A1", "book_a", "a1"),
        _meta_doc("B1", "book_b", "b1"),
        _meta_doc("A2", "book_a", "a2"),
        _meta_doc("A3", "book_a", "a3"),
        _meta_doc("B2", "book_b", "b2"),
        _meta_doc("A4", "book_a", "a4"),
        _meta_doc("B3", "book_b", "b3"),
    ]
    docs = select_contexts_with_book_quota(ranked, ["book_a", "book_b"], k=6)
    counts = {
        source: sum(1 for d in docs if d.metadata["source"] == source)
        for source in ("book_a", "book_b")
    }
    assert len(docs) == 6
    assert counts["book_a"] >= 2 and counts["book_b"] >= 2
    assert counts["book_a"] <= 4 and counts["book_b"] <= 4


def test_quota_same_source_titles_treated_as_one_book():
    ranked = [
        _meta_doc("P1", "个人实相(上).txt", "p1"),
        _meta_doc("P2", "个人实相(上).txt", "p2"),
        _meta_doc("C1", "其他书.txt", "c1"),
        _meta_doc("P3", "个人实相(上).txt", "p3"),
    ]
    docs = select_contexts_with_book_quota(
        ranked, ["个人实相", "个人实相(上)"], k=3
    )
    assert [d.page_content for d in docs] == ["P1", "P2", "P3"]


def test_title_alias_matches_translated_title_and_essays():
    assert title_matches_source("造物的七个奥秘", "1_The Seven Mysteries.doc")
    assert title_matches_source(
        "实践论",
        "《毛泽东选集》（毛选1-7：原版五卷+静火+赤旗+草堂） (毛泽东) (Z-Library).pdf",
    )
    assert title_matches_source(
        "矛盾论", "毛泽东选集一至七卷 (毛泽东) (Z-Library).epub"
    )
    assert not title_matches_source("不存在的书", "某书.txt")


def test_broad_meta_and_front_matter_helpers():
    assert is_broad_question(
        "《个人实相》与《群体实相》对「信念如何塑造经验」的论述有什么异同？"
    )
    assert is_broad_question("《灵魂永生》主要探讨什么主题？")
    assert not is_broad_question("3 的 5 次方加上 7 的平方等于多少？")
    assert is_meta_question(
        "《江青十年讲话汇编》收录的讲话时间跨度和版本信息是什么？"
    )
    assert is_meta_question("《扬升书》是如何写成的、与谁对话？")
    assert not is_meta_question("为什么说瑜珈是单纯的科学？")
    assert is_front_matter_like("序言 本书作者……")
    assert not is_front_matter_like("这是正文，讨论商品的两个因素。")


def test_parse_book_queries_json():
    items = DeepSeekChat._parse_book_queries(
        '```json\n[{"book": "A", "query": "q1"}, {"book": "B", "query": "q2"}]\n```'
    )
    assert [item["book"] for item in items] == ["A", "B"]
    assert DeepSeekChat._parse_book_queries("not json") == []
