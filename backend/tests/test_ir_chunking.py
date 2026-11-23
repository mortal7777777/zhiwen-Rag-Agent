"""IR 切分：跨页续接、课件页边界、表格原子块与表头重复子块。"""

from __future__ import annotations

from langchain_core.documents import Document

from app.config import Settings
from app.rag.ir import (
    build_parents,
    html_table_to_markdown,
    parse_page_blocks,
    split_table_children,
)
from app.rag.service import RAGService


def test_html_table_to_markdown_keeps_cells():
    html = "<table><tr><th>项目</th><th>值</th></tr><tr><td>A</td><td>1</td></tr></table>"
    md = html_table_to_markdown(html)
    assert md.startswith("| 项目 | 值 |")
    assert "| A | 1 |" in md


def test_parse_blocks_splits_question_and_table():
    text = "# 平均数\n\n【例1】题干甲。\n\n【例2】题干乙。\n\n<table><tr><td>A</td><td>1</td></tr></table>"
    blocks = parse_page_blocks(text, 3)
    kinds = [b.kind for b in blocks]
    assert kinds[0] == "heading"
    assert "table" in kinds
    assert sum(1 for b in blocks if b.kind == "paragraph") == 2


def test_cross_page_parent_merges_continuation():
    blocks = []
    blocks.extend(parse_page_blocks("# 第一章\n\n第一页写到一半，", 1))
    blocks.extend(parse_page_blocks("第二页接着写完。\n\n【例2】下一题。", 2))
    parents = build_parents(blocks, layout="book")
    assert len(parents) == 2
    assert parents[0].pages == [1, 2]
    assert "第一页" in parents[0].text and "第二页" in parents[0].text
    assert parents[1].text.startswith("【例2】")


def test_slide_layout_flushes_at_page_boundary():
    page1 = parse_page_blocks("# 第一页\n\n本页结束。", 1)
    page2 = parse_page_blocks("# 第二页\n\n新内容。", 2)
    parents = build_parents(page1 + page2, layout="slide")
    assert [p.pages for p in parents] == [[1], [2]]


def test_table_is_atomic_and_children_repeat_header():
    blocks = parse_page_blocks(
        "# 资料分析\n\n| 项目 | 2023 | 2024 |\n|---|---|---|\n"
        "| 收入 | 120 | 150 |\n| 成本 | 80 | 95 |\n| 利润 | 40 | 55 |",
        7,
    )
    parents = build_parents(blocks, layout="book")
    table_parents = [p for p in parents if p.is_table]
    assert len(table_parents) == 1
    assert "| 项目 | 2023 | 2024 |" in table_parents[0].text
    chunks = split_table_children(table_parents[0].text, child_size=40)
    assert len(chunks) >= 1
    assert all("| 项目 | 2023 | 2024 |" in chunk for chunk in chunks)


def _service(tmp_path) -> RAGService:
    settings = Settings(data_dir=tmp_path / "data", meta_dir=tmp_path / "meta")
    settings.ensure_dirs()
    return RAGService(settings)


def test_service_split_parent_child_uses_ir(tmp_path):
    service = _service(tmp_path)
    docs = [
        Document(
            page_content="# 第一章\n\n第一页写到一半，",
            metadata={
                "source": "a.pdf",
                "relative_path": "a.pdf",
                "page": 1,
                "parse_backend": "paddle",
                "doc_layout": "book",
            },
        ),
        Document(
            page_content="第二页接着写完。",
            metadata={
                "source": "a.pdf",
                "relative_path": "a.pdf",
                "page": 2,
                "parse_backend": "paddle",
                "doc_layout": "book",
            },
        ),
    ]
    parents, children = service._split_parent_child(docs)
    assert children
    assert any(p.metadata.get("pages") == [1, 2] for p in parents)


def test_service_sorts_pages_before_cross_page_merge(tmp_path):
    """页文档乱序（稀疏页 OCR 追加等）不能污染父块文本顺序。"""
    service = _service(tmp_path)

    def page_doc(page: int, marker: str) -> Document:
        return Document(
            page_content=marker + "内容" * 60,
            metadata={
                "source": "a.pdf",
                "relative_path": "a.pdf",
                "page": page,
                "doc_layout": "book",
            },
        )

    docs = [page_doc(1, "AAA"), page_doc(3, "CCC"), page_doc(2, "BBB")]
    parents, _ = service._split_parent_child(docs)
    text = "\n\n".join(p.page_content for p in parents)
    assert text.index("AAA") < text.index("BBB") < text.index("CCC")


def test_insert_image_markers_replaces_tags_in_order():
    from app.rag.ir import insert_image_markers

    md = "前文<div><img src='a.png'/></div>中间<img src='b.png'/>后文"
    out, placed = insert_image_markers(
        md, ["p1_img00", "p1_img01"], {"p1_img00": "图一文字"}
    )
    assert "[[IMG:p1_img00]]\n图一文字" in out
    assert "[[IMG:p1_img01]]" in out
    assert "<img" not in out and "<div" not in out
    assert placed == ["p1_img00", "p1_img01"]

    # 被丢弃的图片块（None）对应标签直接移除
    out2, placed2 = insert_image_markers(
        "<div><img/></div><div><img/></div>", ["id0", None]
    )
    assert "[[IMG:id0]]" in out2 and out2.count("[[IMG:") == 1
    assert placed2 == ["id0"]


def test_parent_text_strips_markers_but_keeps_image_refs(tmp_path):
    service = _service(tmp_path)
    docs = [
        Document(
            page_content="# 第一章\n\n题干文字。\n\n[[IMG:p0001_img00]]\n图内文字",
            metadata={
                "source": "a.pdf",
                "relative_path": "a.pdf",
                "page": 1,
                "parse_backend": "paddle",
                "doc_layout": "book",
                "image_refs": [{"id": "p0001_img00", "path": "ocr_cache/a.png"}],
            },
        )
    ]
    parents, _ = service._split_parent_child(docs)
    assert parents
    assert all("[[IMG:" not in p.page_content for p in parents)
    assert any("图内文字" in p.page_content for p in parents)
    assert any(p.metadata.get("image_refs") for p in parents)


def test_parents_respect_target_soft_limit():
    text = "\n\n".join("段落" + "内" * 100 for _ in range(12))
    blocks = parse_page_blocks(text, 1)
    parents = build_parents(blocks, target=300, max_size=900, layout="book")
    assert len(parents) >= 3
    assert all(len(p.text) <= 900 for p in parents)
    assert any(len(p.text) >= 300 for p in parents)


def test_insert_image_markers_skips_formula_images():
    from app.rag.ir import insert_image_markers

    md = (
        "<div><img src='img_in_formula_box_1_2_3_4.jpg'/></div>"
        "<div><img src='img_in_image_box_5_6_7_8.jpg'/></div>"
    )
    out, placed = insert_image_markers(md, ["p1_img00"])
    assert "formula" not in out and "[[IMG:p1_img00]]" in out
    assert placed == ["p1_img00"]
