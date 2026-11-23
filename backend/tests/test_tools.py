"""Agent 工具纯函数单测：联网结果可信度标注。"""

from __future__ import annotations

from langchain_core.documents import Document

from app.agent.tools import _credibility_for, execute_knowledge_search
from app.permissions import describe_tool_call, is_sensitive_tool


def test_credibility_high():
    assert _credibility_for("https://www.reuters.com/world/")[0] == "high"
    assert _credibility_for("https://www.gov.cn/zhengce/")[0] == "high"
    assert _credibility_for("https://news.bbc.com/")[0] == "high"


def test_credibility_medium():
    assert _credibility_for("https://www.zhihu.com/question/1")[0] == "medium"
    assert _credibility_for("https://example.com/unknown")[0] == "medium"


def test_credibility_low():
    assert _credibility_for("https://www.reddit.com/r/tech/")[0] == "low"
    assert _credibility_for("https://t.co/abc")[0] == "low"
    assert _credibility_for("")[0] == "low"


def test_add_document_is_sensitive():
    assert is_sensitive_tool("add_document", {})
    desc = describe_tool_call("add_document", {"filename": "notes.md"})
    assert "notes.md" in desc and "知识库" in desc


class _FakeRetriever:
    def __init__(self, docs: list[Document]):
        self.docs = docs

    def invoke(self, _query: str) -> list[Document]:
        return self.docs


def test_knowledge_search_keeps_all_results_and_full_parent_text():
    """回归：工具层不得再把检索层给的多书/宽泛题结果腰斩到 3 条或 550 字。"""
    content = "甲" * 900  # parent_max_chunk_size 上限内的完整段落
    docs = [
        Document(page_content=content, metadata={"source": f"book-{i}"})
        for i in range(8)
    ]
    counter = [0]

    result = execute_knowledge_search("对比这几本书", _FakeRetriever(docs), counter)

    assert len(result["results"]) == 8
    assert counter[0] == 8
    assert all(item["content"].endswith(content) for item in result["results"])
    assert all("…" not in item["content"] for item in result["results"])
