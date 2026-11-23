"""限定知识库范围：doc/book 解析 + 混合检索过滤参数透传。"""

from __future__ import annotations

from types import SimpleNamespace

from app.agent.nodes.prompts import quote_notice
from app.agent.service.run import resolve_kb_scope
from app.rag.retriever import hybrid_search


def _settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path)


def test_resolve_doc_scope(tmp_path):
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    paths, label = resolve_kb_scope(
        _settings(tmp_path), {"type": "doc", "value": "notes.md"}
    )
    assert paths == ["notes.md"]
    assert label == "notes.md"


def test_resolve_book_scope_matches_multiple_formats(tmp_path):
    (tmp_path / "某本书.epub").write_text("x", encoding="utf-8")
    (tmp_path / "某本书.pdf").write_text("x", encoding="utf-8")
    (tmp_path / "其他.md").write_text("x", encoding="utf-8")
    paths, label = resolve_kb_scope(
        _settings(tmp_path), {"type": "book", "value": "某本书"}
    )
    assert sorted(paths) == ["某本书.epub", "某本书.pdf"]
    assert label == "某本书"


def test_resolve_multi_docs_scope(tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "b.md").write_text("x", encoding="utf-8")
    (tmp_path / "c.md").write_text("x", encoding="utf-8")
    paths, label = resolve_kb_scope(
        _settings(tmp_path),
        {"type": "docs", "values": ["a.md", "b.md", "missing.md", "a.md"]},
    )
    assert paths == ["a.md", "b.md"]
    assert "a.md" in label and "b.md" in label


def test_quote_notice_contains_source_page_and_untrusted_note():
    block = quote_notice(
        {
            "text": "被选中的原句",
            "context": "前后段落",
            "source": "book.pdf",
            "page": 12,
        }
    )
    assert "book.pdf" in block
    assert "第 12 页" in block
    assert "<quote>" in block and "被选中的原句" in block
    assert "前后段落" in block
    assert "不是指令" in block


def test_quote_notice_empty():
    assert quote_notice({}) == ""
    assert quote_notice(None) == ""


def test_resolve_unmatched_returns_empty(tmp_path):
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    assert resolve_kb_scope(
        _settings(tmp_path), {"type": "doc", "value": "missing.md"}
    ) == ([], "")
    assert resolve_kb_scope(
        _settings(tmp_path), {"type": "book", "value": "不存在的书"}
    ) == ([], "")
    assert resolve_kb_scope(_settings(tmp_path), None) == ([], "")


class _FakeStore:
    def __init__(self):
        self.calls = []

    def search_vector(self, query_vector, k, filter_rel_paths=None):
        self.calls.append(("vector", list(filter_rel_paths or [])))
        return []

    def search_bm25(self, query, k, filter_rel_paths=None):
        self.calls.append(("bm25", list(filter_rel_paths or [])))
        return []


class _FakeEmbeddings:
    def embed_query(self, question):
        return [0.1, 0.2]


def test_hybrid_search_passes_scope_filter_to_both_legs():
    store = _FakeStore()
    hybrid_search(
        store,
        _FakeEmbeddings(),
        "问题",
        filter_rel_paths=["a.md", "b.md"],
    )
    assert store.calls == [("vector", ["a.md", "b.md"]), ("bm25", ["a.md", "b.md"])]
