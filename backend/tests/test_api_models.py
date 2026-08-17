"""API 嵌入 / API 重排序客户端单测（mock httpx，不依赖网络与 GPU）。"""

from __future__ import annotations

from langchain_core.documents import Document

from app.agent.tools import _search_searxng, execute_web_search
from app.mcp_manager import _mcp_default_args
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
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeHttpxGet:
    """替换 httpx.get 的替身（SearXNG 走 GET /search?format=json）。"""

    def __init__(self, data: dict):
        self._data = data
        self.calls: list[str] = []

    def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        self.last_headers = headers or {}
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
    # 带转发头（绕过 SearXNG limiter 对无头请求的 403）
    assert fake.last_headers.get("X-Forwarded-For") == "127.0.0.1"


def test_search_searxng_engines_param(monkeypatch):
    """engines 参数拼进 URL（指定引擎避免 google 系超时拉低质量）。"""
    import httpx as real_httpx

    fake = FakeHttpxGet({"results": [{"title": "T", "url": "https://x.com", "content": "C"}]})
    monkeypatch.setattr(real_httpx, "get", fake.get)
    _search_searxng("测试", "http://localhost:8889", 5, engines="bing,baidu,sogou")
    assert "engines=bing%2Cbaidu%2Csogou" in fake.calls[0]
    # 空引擎不拼参数
    fake2 = FakeHttpxGet({"results": [{"title": "T", "url": "https://x.com", "content": "C"}]})
    monkeypatch.setattr(real_httpx, "get", fake2.get)
    _search_searxng("测试", "http://localhost:8889", 5, engines="")
    assert "engines=" not in fake2.calls[0]


def test_search_searxng_empty_results_reports_dead_engines(monkeypatch):
    """空结果 + unresponsive_engines 时抛出含诊断信息的错误。"""
    import httpx as real_httpx

    fake = FakeHttpxGet(
        {
            "results": [],
            "unresponsive_engines": [
                ["google cse", "超时"],
                ["duckduckgo", "超时"],
                ["brave", "请求过于频繁"],
            ],
        }
    )
    monkeypatch.setattr(real_httpx, "get", fake.get)
    try:
        _search_searxng("测试", "http://localhost:8889", 5)
        assert False, "空结果应抛错"
    except RuntimeError as exc:
        msg = str(exc)
        assert "未返回结果" in msg
        assert "google cse" in msg
        assert "请求过于频繁" in msg
        assert "bing,baidu,sogou" in msg  # 给出引擎建议


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


def test_search_searxng_403_gives_actionable_hint(monkeypatch):
    """403 时报错信息包含两个必做配置提示（JSON format + 代理）。"""
    import httpx as real_httpx

    class ForbiddenResponse:
        status_code = 403

        def raise_for_status(self):
            raise RuntimeError("403")

    monkeypatch.setattr(
        real_httpx, "get", lambda *a, **k: ForbiddenResponse()
    )
    try:
        _search_searxng("q", "http://localhost:8888", 3)
        assert False, "403 应抛错"
    except RuntimeError as exc:
        msg = str(exc)
        assert "403" in msg
        assert "settings.yml" in msg  # JSON 格式配置提示
        assert "7890" in msg  # 代理端口提示


# ---------------- 正文 XML 工具调用泄漏清理 ----------------

from app.agent.langgraph_agent import _strip_xml_tool_tags


def test_strip_xml_tool_tags_removes_tool_call_markup():
    text = (
        "我先检索一下。"
        "<tool_calls><invoke name=\"knowledge_base_search\">"
        "<parameter name=\"query\">测试</parameter></invoke></tool_calls>"
        "结论如下：……"
    )
    cleaned = _strip_xml_tool_tags(text)
    assert "<tool_calls>" not in cleaned
    assert "<invoke" not in cleaned
    assert "<parameter" not in cleaned
    assert "我先检索一下" in cleaned
    assert "结论如下" in cleaned


def test_strip_xml_tool_tags_variants_and_preserves_whitespace():
    # tool_use / function_calls / <user|tool_calls> 变体都清掉
    text = "<tool_use name=\"bash\"><parameter name=\"command\">ls</parameter></tool_use>"
    assert "<tool_use" not in _strip_xml_tool_tags(text)
    text2 = "<user|tool_calls><invoke name=\"x\"></invoke></user|tool_calls>"
    assert "<" not in _strip_xml_tool_tags(text2).replace("|", "")
    # 不做 strip：保留首尾空白（流式分片安全）
    raw = "  <invoke name=\"a\"></invoke>  "
    assert _strip_xml_tool_tags(raw) == "    "
    # 普通文本不受影响
    assert _strip_xml_tool_tags("正常回答 <div>标签</div>") == "正常回答 <div>标签</div>"


def test_strip_xml_tool_tags_removes_whole_block_content():
    # 整块删除：内部参数值（命令/query）也不得泄漏（仅删标签会留下参数内容）
    text = (
        "我先检索一下。<tool_calls><invoke name=\"knowledge_base_search\">"
        "<parameter name=\"query\">机密检索词</parameter></invoke></tool_calls>"
        "结论如下：……"
    )
    cleaned = _strip_xml_tool_tags(text)
    assert "机密检索词" not in cleaned  # 参数内容不泄漏
    assert "<tool_calls" not in cleaned
    assert "我先检索一下" in cleaned
    assert "结论如下" in cleaned

    # 跨分片残留：外层标签不完整时，完整子块仍被整块删除 + 孤立标签清掉
    partial = "回答<tool_calls><invoke name=\"bash\">ls</invoke>（后续）"
    cleaned3 = _strip_xml_tool_tags(partial)
    assert "ls" not in cleaned3
    assert "<" not in cleaned3.replace("（", "").replace("）", "")
    assert "回答" in cleaned3 and "后续" in cleaned3

    # <|tool_calls> 变体整块删除
    pipe_variant = (
        "前置<|tool_calls><invoke name=\"bash\">"
        "<parameter name=\"command\">dir</parameter></invoke></|tool_calls>后置"
    )
    cleaned4 = _strip_xml_tool_tags(pipe_variant)
    assert "dir" not in cleaned4
    assert "<" not in cleaned4.replace("|", "")
    assert cleaned4 == "前置后置"


def test_strip_xml_tool_tags_fullwidth_dsml_variant():
    # 实测泄漏格式：标签用全角竖线 ｜（U+FF5C）+ |DSML| 前缀，
    # 不归一化时检测/清洗全部漏判，正文带标签直接进最终回答
    text = (
        "继续排查。<｜DSML｜tool_calls>\n"
        "<｜DSML｜invoke name=\"read_file\">\n"
        "<｜DSML｜parameter name=\"limit\">40</｜DSML｜parameter>\n"
        "</｜DSML｜invoke>\n"
        "</｜DSML｜tool_calls>后置"
    )
    cleaned = _strip_xml_tool_tags(text)
    assert "<" not in cleaned
    assert "tool_calls" not in cleaned
    assert "invoke" not in cleaned
    assert "继续排查" in cleaned and "后置" in cleaned


def test_parse_xml_tool_calls_robust_variants():
    from app.agent.langgraph_agent import _parse_xml_tool_calls

    # 全角竖线 + |DSML| 前缀变体（<｜DSML｜invoke>）同样能解析执行
    calls3 = _parse_xml_tool_calls(
        "<｜DSML｜tool_calls><｜DSML｜invoke name=\"read_file\">"
        "<｜DSML｜parameter name=\"limit\">40</｜DSML｜parameter>"
        "<｜DSML｜parameter name=\"offset\">1915</｜DSML｜parameter>"
        "</｜DSML｜invoke></｜DSML｜tool_calls>"
    )
    assert len(calls3) == 1
    assert calls3[0]["name"] == "read_file"
    assert calls3[0]["args"] == {"limit": 40, "offset": 1915}

    # 容忍 name= 两侧空格与单引号；数字/布尔 JSON 解析；null 参数跳过
    calls = _parse_xml_tool_calls(
        "<tool_calls><invoke name = 'browser_snapshot'>"
        "<parameter name='depth'>10</parameter>"
        "<parameter name='boxes'>true</parameter>"
        "<parameter name='filename'>null</parameter>"
        "</invoke></tool_calls>"
    )
    assert len(calls) == 1
    assert calls[0]["name"] == "browser_snapshot"
    assert calls[0]["args"] == {"depth": 10, "boxes": True}  # null 被跳过

    # antml:invoke 变体 + 普通字符串参数（不能误解析成 JSON）
    calls2 = _parse_xml_tool_calls(
        "<antml:invoke name=\"bash\">"
        "<parameter name=\"command\">ls -la</parameter></antml:invoke>"
    )
    assert calls2 and calls2[0]["name"] == "bash"
    assert calls2[0]["args"] == {"command": "ls -la"}


def test_tool_has_required_args_schema_aware():
    from pydantic import Field, create_model

    from app.agent.langgraph_agent import _tool_has_required_args

    class _StubTool:
        def __init__(self, schema):
            self.args_schema = schema

    # MCP 工具形态（_schema_to_model 全可选）→ 空参合法，放行到 invoke 补默认值
    opt = create_model(
        "OptArgs",
        target=(str, Field(default=None)),
        depth=(int, Field(default=None)),
    )
    assert _tool_has_required_args(_StubTool(opt)) is False

    # 有必填字段 → 拦截空参（原有"参数缺失"兜底保留）
    req = create_model("ReqArgs", query=(str, Field(...)))
    assert _tool_has_required_args(_StubTool(req)) is True

    # 无 schema 信息 → 保守拦截（保持原有行为）
    assert _tool_has_required_args(_StubTool(None)) is True

    # schema 无字段 → 空参合法
    assert _tool_has_required_args(_StubTool(create_model("EmptyArgs"))) is False


# ---------------- MCP 工具默认参数（Playwright schema 缺陷兜底） ----------------


def test_mcp_default_args_browser_snapshot():
    # 无参调用 → 自动补全 4 个参数（服务端按必填校验）
    args = _mcp_default_args("browser_snapshot", {})
    assert args["target"] == "body"
    assert args["depth"] == 10
    assert args["boxes"] is False
    assert args["filename"].startswith("snapshot-")  # 自动命名
    assert args["filename"].endswith(".md")
    # StructuredTool 经 pydantic 解析后可选字段会填 None（回归：曾误把
    # None 当作"已显式传参"跳过补默认，导致服务端收到 null）
    args3 = _mcp_default_args(
        "browser_snapshot", {"target": None, "depth": None, "boxes": None}
    )
    assert args3["target"] == "body"
    assert args3["depth"] == 10
    assert args3["boxes"] is False
    assert args3["filename"].startswith("snapshot-")
    # 显式传的参数不被覆盖
    args2 = _mcp_default_args("browser_snapshot", {"target": "main", "filename": "x.md"})
    assert args2["target"] == "main"
    assert args2["filename"] == "x.md"
    assert args2["depth"] == 10


def test_schema_to_model_honors_default_in_required():
    # Playwright MCP 的 console_messages/network_requests：required 字段带
    # default（服务端空参可用默认值），客户端不应按严格必填解析，
    # 否则 _tool_has_required_args 会误拦空参调用
    from app.mcp_manager import _schema_to_model
    from app.agent.langgraph_agent import _tool_has_required_args

    schema = {
        "type": "object",
        "properties": {
            "level": {"type": "string", "default": "info"},
            "all": {"type": "boolean"},
        },
        "required": ["level"],
    }
    model = _schema_to_model(schema)
    parsed = model.model_validate({})
    assert parsed.level == "info"  # 默认值生效，空参可解析
    assert parsed.all is None

    class _Stub:
        args_schema = model

    class _Stub2:
        def __init__(self, schema):
            self.args_schema = schema

    assert _tool_has_required_args(_Stub()) is False  # 不拦截空参

    # 无 default 的必填字段仍按必填（原行为不变）
    schema2 = {"type": "object", "properties": {"query": {"type": "string"}},
               "required": ["query"]}
    model2 = _schema_to_model(schema2)
    assert _tool_has_required_args(_Stub2(model2)) is True
    try:
        model2.model_validate({})
        raise AssertionError("应拒绝缺 query")
    except Exception:
        pass


def test_mcp_default_args_other_tools_untouched():
    # 未登记的工具不补参
    assert _mcp_default_args("browser_navigate", {"url": "https://x.com"}) == {
        "url": "https://x.com"
    }
    assert _mcp_default_args("unknown_tool", {}) == {}


def test_mcp_default_args_strips_null_optional_fields():
    # browser_tabs：schema 仅 action 必填，服务端（zod .optional()）拒绝显式
    # null 但接受缺省——pydantic 解析出的 null 必须剔除（曾实测报
    # "expected number, received null → at index/url"）；兜底默认值补占位
    args = _mcp_default_args(
        "browser_tabs", {"action": "list", "index": None, "url": None}
    )
    assert args["action"] == "list"
    assert args["index"] == 0 and args["url"] == ""  # 占位默认，无 null
    assert all(v is not None for v in args.values())
    # browser_wait_for：空参补 1 秒等待（服务端要求三选一）
    wf = _mcp_default_args("browser_wait_for", {"time": None, "text": None, "textGone": None})
    assert wf == {"time": 1}
    # 显式传 text 时 time 兜底不影响条件等待
    wf2 = _mcp_default_args("browser_wait_for", {"text": "加载完成", "time": None})
    assert wf2["text"] == "加载完成" and wf2["time"] == 1
    # 未登记的工具同样剔除 null（通用兜底）
    assert _mcp_default_args("browser_navigate", {"url": None, "timeout": None}) == {}
    # 非 None 值保留，null 剔除
    assert _mcp_default_args(
        "browser_navigate", {"url": "https://x.com", "timeout": None}
    ) == {"url": "https://x.com"}
