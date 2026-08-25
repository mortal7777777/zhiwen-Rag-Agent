"""块级去重单测：跨文档重复 child 只保留首次出现的一份。"""

from __future__ import annotations

from langchain_core.documents import Document

from app.config import get_settings
from app.rag.service import RAGService


def _doc(source: str, text: str) -> Document:
    return Document(page_content=text, metadata={"source": source})


def _split(texts: dict[str, str]):
    service = RAGService(get_settings())
    documents = [_doc(src, text) for src, text in texts.items()]
    parents, children = service._split_parent_child(documents)
    return parents, children


def test_identical_documents_dedup_to_one_child():
    """两本书内容完全相同：child 只保留第一份，parent 两份都在。"""
    text = "这是完全相同的段落内容。" * 20  # 280 字,整块作为一个 child
    parents, children = _split({"book_a": text, "book_b": text})

    assert len(children) == 1
    assert children[0].metadata["source"] == "book_a"
    assert len(parents) == 2


def test_partial_overlap_keeps_first_source():
    """部分重复：共同段落 A 只保留一份且归属首次出现的书，非重复段落 B/C 不受影响。"""
    # 每段约 200 字：单段 < 320 成块，但两段合起来 > 320 必然拆成两个 child
    a_seg = "共同段落甲。" + "甲" * 190   # ~196 字
    b_seg = "独有段落乙。" + "乙" * 190   # ~196 字
    c_seg = "独有段落丙。" + "丙" * 190   # ~196 字
    doc1_text = f"{a_seg}\n\n{b_seg}"
    doc2_text = f"{a_seg}\n\n{c_seg}"

    parents, children = _split({"book_a": doc1_text, "book_b": doc2_text})

    contents = [c.page_content for c in children]
    a_count = sum(1 for c in contents if c == a_seg)
    assert a_count == 1
    assert b_seg in contents and c_seg in contents
    dup = [c for c in children if c.page_content == a_seg]
    assert dup and dup[0].metadata["source"] == "book_a"
    assert len(parents) == 2


def test_duplicates_within_same_document_also_deduped():
    """同一文档内重复段落同样被去重（示例书式反复出现的段落）。"""
    seg = "反复出现的段落。" * 20  # ~200 字
    parents, children = _split({"book_a": f"{seg}\n\n{seg}"})

    assert len(children) == 1
    assert len(parents) == 1
