"""API 嵌入 / API 重排序客户端单测（mock httpx，不依赖网络与 GPU）。"""

from __future__ import annotations

from langchain_core.documents import Document

from app.agent.tools import _search_searxng, execute_web_search
from app.rag.embeddings import APIBGEEmbeddings
from app.rag.reranker import APIReranker


class FakeResponse:
    def __init__(self, data: dict, status: int = 200):
        self._data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class FakeHttpx:
    """记录请求并返回预设响应的 httpx.post 替身。"""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict, dict]] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json or {}, headers or {}))
        return FakeResponse(self._responses.pop(0))


def _install_fake_httpx(monkeypatch, responses: list[dict]) -> FakeHttpx:
    """替换全局 httpx.post（API 类函数内 import httpx 拿的是 sys.modules 全局）。"""
    import httpx as real_httpx

    fake = FakeHttpx(responses)
    monkeypatch.setattr(real_httpx, "post", fake.post)
    return fake


def test_api_embeddings_query_and_dimension(monkeypatch):
    fake = _install_fake_httpx(
        monkeypatch,
        [
            {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
            {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
        ],
    )
    emb = APIBGEEmbeddings("https://api.example.com/v1", "sk-123", model="bge-m3")
    assert emb.dimension == 3  # 探测触发一次真实调用
    vec = emb.embed_query("测试")
    assert vec == [0.1, 0.2, 0.3]
    url, body, headers = fake.calls[0]
    assert url == "https://api.example.com/v1/embeddings"
    assert body["model"] == "bge-m3"
    assert headers["Authorization"] == "Bearer sk-123"
    assert emb.device == "api"


def test_api_embeddings_batch_keeps_order(monkeypatch):
    # 供应商乱序返回（index 2 在 index 0 前面），按 index 排序后保持输入顺序
    fake = _install_fake_httpx(
        monkeypatch,
        [
            {
                "data": [
                    {"index": 2, "embedding": [2.0]},
                    {"index": 0, "embedding": [0.0]},
                    {"index": 1, "embedding": [1.0]},
                ]
            }
        ],
    )
    emb = APIBGEEmbeddings("https://api.example.com/v1", "")
    vectors = emb.embed_documents(["a", "b", "c"], batch_size=3)
    assert vectors == [[0.0], [1.0], [2.0]]
    # 无 api_key 时不带 Authorization 头
    _url, _body, headers = fake.calls[0]
    assert "Authorization" not in headers


def test_api_embeddings_batch_splitting(monkeypatch):
    # 5 条文本、batch=2 → 3 次请求
    fake = _install_fake_httpx(
        monkeypatch,
        [
            {"data": [{"index": i, "embedding": [float(i)]} for i in range(2)]},
            {"data": [{"index": i, "embedding": [float(i + 2)]} for i in range(2)]},
            {"data": [{"index": 0, "embedding": [4.0]}]},
        ],
    )
    emb = APIBGEEmbeddings("https://api.example.com/v1", "")
    vectors = emb.embed_documents(["a", "b", "c", "d", "e"], batch_size=2)
    assert len(vectors) == 5
    assert len(fake.calls) == 3
    assert [len(c[1]["input"]) for c in fake.calls] == [2, 2, 1]


def test_api_reranker_orders_by_score(monkeypatch):
    # 3 个候选，API 返回第 2 个最相关 → 排序后在前
    fake = _install_fake_httpx(
        monkeypatch,
        [
            {
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.4},
                    {"index": 2, "relevance_score": 0.7},
                ]
            }
        ],
    )
    rr = APIReranker("https://api.example.com/v1", "sk-1", model="bge-reranker-v2-m3")
    docs = [Document(page_content=f"doc{i}") for i in range(3)]
    ranked = rr.rerank("query", docs, top_k=2)
    assert [d.page_content for d in ranked] == ["doc1", "doc2"]
    assert ranked[0].metadata["rerank_score"] == 0.9
    url, body, _h = fake.calls[0]
    assert url == "https://api.example.com/v1/rerank"
    assert body["documents"] == ["doc0", "doc1", "doc2"]
    assert body["top_n"] == 2
    assert body["model"] == "bge-reranker-v2-m3"


def test_api_reranker_fills_unscored(monkeypatch):
    # API 只为部分候选打分：未打分的按原顺序补位到 top_k
    fake = _install_fake_httpx(monkeypatch, [{"results": [{"index": 1, "relevance_score": 0.8}]}])
    rr = APIReranker("https://api.example.com/v1", "")
    docs = [Document(page_content=f"doc{i}") for i in range(3)]
    ranked = rr.rerank("query", docs, top_k=3)
    assert len(ranked) == 3
    assert ranked[0].page_content == "doc1"  # 最高分在前
    assert set(d.page_content for d in ranked) == {"doc0", "doc1", "doc2"}


# ---------------- SearXNG 自托管搜索 ----------------

class FakeGetResponse:
    def __init__(self, data: dict):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeHttpxGet:
    """替换 httpx.get 的替身（SearXNG 走 GET /search?format=json）。"""

    def __init__(self, data: dict):
        self._data = data
        self.calls: list[str] = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        return FakeGetResponse(self._data)


def test_search_searxng(monkeypatch):
    import httpx as real_httpx

    fake = FakeHttpxGet(
        {
            "results": [
                {"title": "标题一", "url": "https://a.example.com/1", "content": "内容一"},
                {"title": "标题二", "url": "https://b.example.com/2"},
            ]
        }
    )
    monkeypatch.setattr(real_httpx, "get", fake.get)
    results = _search_searxng("测试", "http://localhost:8888", max_results=5)
    assert len(results) == 2
    assert results[0]["title"] == "标题一"
    assert results[0]["snippet"] == "内容一"
    # 无 content 时回退 snippet 字段（SearXNG 两种字段名都兼容）
    assert results[1]["snippet"] == ""
    # URL 带 format=json 且编码了查询
    assert fake.calls[0].startswith("http://localhost:8888/search?")
    assert "format=json" in fake.calls[0]
    assert "q=" in fake.calls[0]


def test_search_searxng_requires_base_url(monkeypatch):
    import httpx as real_httpx

    monkeypatch.setattr(real_httpx, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应发请求")))
    try:
        _search_searxng("q", "", 3)
        assert False, "空 base_url 应抛错"
    except RuntimeError as exc:
        assert "SearXNG" in str(exc)


def test_execute_web_search_searxng_branch(monkeypatch):
    import httpx as real_httpx

    fake = FakeHttpxGet(
        {"results": [{"title": "T", "url": "https://news.example.com/x", "content": "C"}]}
    )
    monkeypatch.setattr(real_httpx, "get", fake.get)
    result = execute_web_search(
        "今日新闻", "searxng", api_key="", max_results=3, searxng_base_url="http://localhost:8888"
    )
    assert len(result["results"]) == 1
    item = result["results"][0]
    assert item["credibility"] == "medium"  # 走可信度评估
    assert "credibility_reason" in item
    assert result["summary"].startswith("联网搜索到")
