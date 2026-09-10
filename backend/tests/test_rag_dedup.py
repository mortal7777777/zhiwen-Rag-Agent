"""块级去重单测：去重作用域＝单文件内（跨文件重复保留各自副本）。

2026-09-10 语义变更（docs/INDEX_REDESIGN_PLAN.md §1.4）：原"跨文档全局去重"
与 per-doc 删除冲突（删一个文件会牵连另一文件里同段落的可检索性），去重改为
每文件独立；检索端的重复占坑由候选 source 分流接管。
"""

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


def test_identical_documents_keep_own_chunks():
    """两本书内容完全相同：各自保留自己的 child（各删各的，互不牵连）。"""
    text = "这是完全相同的段落内容。" * 20  # 280 字，整块作为一个 child
    parents, children = _split({"book_a": text, "book_b": text})

    assert len(children) == 2
    assert {c.metadata["source"] for c in children} == {"book_a", "book_b"}
    assert len(parents) == 2


def test_partial_overlap_keeps_both_copies():
    """部分重复：共同段落两份都在，各归属自己的书（不再由首次出现者独占）。"""
    # 每段约 200 字：单段 < 320 成块，但两段合起来 > 320 必然拆成两个 child
    a_seg = "共同段落甲。" + "甲" * 190   # ~196 字
    b_seg = "独有段落乙。" + "乙" * 190   # ~196 字
    c_seg = "独有段落丙。" + "丙" * 190   # ~196 字
    doc1_text = f"{a_seg}\n\n{b_seg}"
    doc2_text = f"{a_seg}\n\n{c_seg}"

    parents, children = _split({"book_a": doc1_text, "book_b": doc2_text})

    contents = [c.page_content for c in children]
    a_count = sum(1 for c in contents if c == a_seg)
    assert a_count == 2
    assert b_seg in contents and c_seg in contents
    a_sources = {c.metadata["source"] for c in children if c.page_content == a_seg}
    assert a_sources == {"book_a", "book_b"}
    assert len(parents) == 2


def test_duplicates_within_same_document_also_deduped():
    """同一文档内重复段落仍被去重（同书内反复出现的段落只留一份）。"""
    seg = "反复出现的段落。" * 20  # ~200 字
    parents, children = _split({"book_a": f"{seg}\n\n{seg}"})

    assert len(children) == 1
    assert len(parents) == 1


def _split_docs(documents: list[Document]):
    service = RAGService(get_settings())
    return service._split_parent_child(documents)


def test_dedup_scope_is_file_not_loaded_unit():
    """PDF 按页 / EPUB 按章加载成多个单元，去重仍按整个文件生效。"""
    seg = "跨页重复的段落。" * 20  # ~200 字
    other = "另一页独有的段落。" * 20
    same_file = {"source": "book.pdf", "relative_path": "book.pdf"}
    _, children = _split_docs(
        [
            Document(page_content=f"{seg}\n\n{other}", metadata=dict(same_file)),
            Document(page_content=seg, metadata=dict(same_file)),
        ]
    )
    contents = [c.page_content for c in children]
    assert sum(1 for c in contents if c == seg) == 1  # 同文件跨单元去重
    assert other in contents
