"""Agent 工具（LangChain @tool 实现）：知识库检索 + 联网搜索。

设计说明：
- 用 langchain_core.tools.tool 装饰器定义工具，交给 Agent 的 ReAct 循环托管；
- 知识库检索把现有 RAG 管道包装成 LangChain Retriever（KnowledgeBaseRetriever）；
- 联网搜索支持 provider 可插拔：duckduckgo（免 key）/ tavily（需 key）/ off。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool, tool
from pydantic import BaseModel, Field

from ..rag.retriever import KnowledgeBaseRetriever
from ..runtime_config import effective_or

logger = logging.getLogger(__name__)


# ---------------- 工具执行（纯函数，便于单测） ----------------

def _truncate(text: str, limit: int = 550) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def execute_knowledge_search(
    query: str,
    retriever: KnowledgeBaseRetriever,
    counter: list[int],
) -> dict:
    """知识库检索：复用现有 RAG 检索管道（查询扩展 + 混合检索 + 精排）。

    counter 是本次对话的全局引用编号器：每条结果带唯一 index，
    模型回答时用 [n] 标注，前端可按编号对应到来源卡片。
    """
    docs = retriever.invoke(query)
    results = []
    for doc in docs[:3]:
        counter[0] += 1
        results.append(
            {
                "index": counter[0],
                "type": "kb",
                "content": _truncate(doc.page_content, 550),
                "score": round(
                    float(
                        doc.metadata.get("rerank_score")
                        or doc.metadata.get("parent_score")
                        or 0.0
                    ),
                    4,
                ),
                "source": doc.metadata.get("source"),
                "page": doc.metadata.get("page"),
                "relative_path": doc.metadata.get("relative_path"),
            }
        )
    return {
        "query": query,
        "results": results,
        "summary": f"知识库检索到 {len(results)} 条相关内容",
    }


def execute_web_search(
    query: str,
    provider: str,
    api_key: str,
    max_results: int,
    searxng_base_url: str = "",
    searxng_engines: str = "",
) -> dict:
    """联网搜索：provider = duckduckgo | tavily | searxng | off。"""
    if provider == "off":
        return {
            "query": query,
            "results": [],
            "summary": "联网搜索已关闭",
            "error": "web search disabled",
        }
    try:
        if provider == "tavily":
            results = _search_tavily(query, api_key, max_results)
        elif provider == "searxng":
            results = _search_searxng(query, searxng_base_url, max_results, searxng_engines)
        else:
            results = _search_with_fallback(query, max_results)
        for item in results:
            credibility, reason = _credibility_for(item.get("url", ""))
            item["credibility"] = credibility
            item["credibility_reason"] = reason
        return {
            "query": query,
            "results": results,
            "summary": f"联网搜索到 {len(results)} 条结果",
        }
    except Exception as exc:
        logger.warning("联网搜索失败（provider=%s）：%s", provider, exc)
        return {
            "query": query,
            "results": [],
            "summary": f"联网搜索失败：{exc}",
            "error": str(exc),
        }


# 网页可信度启发式评估（不消耗 LLM，纯域名规则）
_HIGH_CREDIBILITY_DOMAINS = (
    "wikipedia.org", "gov.cn", "edu.cn", "ac.cn", "who.int", "un.org",
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "nytimes.com",
    "theguardian.com", "nature.com", "science.org", "nejm.org", "pubmed.ncbi.nlm.nih.gov",
    "xinhuanet.com", "people.com.cn", "chinadaily.com.cn", "cctv.com", "nhk.or.jp",
    "arxiv.org",
)
_MEDIUM_CREDIBILITY_DOMAINS = (
    "zhihu.com", "thepaper.cn", "sina.com.cn", "sohu.com", "163.com", "qq.com",
    "medium.com", "csdn.net", "cnblogs.com", "github.com", "gitee.com",
    "stackoverflow.com", "segmentfault.com", "infoq.cn", "36kr.com",
    "baike.baidu.com", "baidu.com",
)
_LOW_CREDIBILITY_DOMAINS = (
    "reddit.com", "twitter.com", "x.com", "weibo.com", "douyin.com", "tiktok.com",
    "bilibili.com", "quora.com", "wordpress.com", "blogspot.com", "medium.com/@",
    "bit.ly", "t.co", "tinyurl.com",
)


def _credibility_for(url: str) -> tuple[str, str]:
    """按来源域名给搜索结果打可信度标签（high / medium / low）。"""
    from urllib.parse import urlparse

    host = (urlparse(url or "").netloc or "").lower()
    if not host:
        return "low", "缺少来源链接"
    if host in _LOW_CREDIBILITY_DOMAINS or host.endswith(tuple("." + d for d in _LOW_CREDIBILITY_DOMAINS)):
        return "low", f"社交/论坛/UGC 类域名：{host}"
    if host.endswith((".gov", ".edu", ".mil", ".gov.cn", ".edu.cn", ".ac.cn")):
        return "high", f"政府/教育/科研机构域名：{host}"
    if host in _HIGH_CREDIBILITY_DOMAINS or host.endswith(tuple("." + d for d in _HIGH_CREDIBILITY_DOMAINS)):
        return "high", f"权威媒体/学术/百科域名：{host}"
    if host in _MEDIUM_CREDIBILITY_DOMAINS or host.endswith(tuple("." + d for d in _MEDIUM_CREDIBILITY_DOMAINS)):
        return "medium", f"门户/技术社区/自媒体平台：{host}"
    return "medium", "未知来源域名，建议交叉验证"


def _search_duckduckgo(query: str, max_results: int) -> list[dict]:
    from ddgs import DDGS

    # region=cn-zh 优先返回中文结果；backend=auto 自适应
    raw = list(
        DDGS().text(query, region="cn-zh", backend="auto", max_results=max_results)
    )
    results = []
    for item in raw:
        title = item.get("title") or ""
        href = item.get("href") or item.get("url") or ""
        snippet = item.get("body") or item.get("snippet") or ""
        if not title and not snippet:
            continue
        results.append(
            {
                "title": _truncate(title, 150),
                "url": href,
                "snippet": _truncate(snippet, 350),
            }
        )
    return results[:max_results]


def _search_with_fallback(query: str, max_results: int) -> list[dict]:
    """DuckDuckGo 优先；失败时依次回退 Bing 国内站 → 百度（均直连）。

    DDGS 底层（primp）会读 Windows 系统代理且无法禁用：Clash 未启动时必然
    报 tunnel error（2026-10-14 实测），且会逐个引擎白等十几秒——系统代理
    不可达时直接跳过 DDGS。降级链里 Bing 国内站最快最稳（0.5s、无验证码），
    百度放末级（对无 cookie 自动化请求偶发 302 图形验证，同日实测）。
    """
    from ..network import proxy_reachable

    if proxy_reachable():
        try:
            results = _search_duckduckgo(query, max_results)
            if results:
                return results
            logger.warning("DuckDuckGo 搜索无结果，尝试 Bing")
        except Exception as exc:
            logger.warning("DuckDuckGo 搜索失败（%s），尝试 Bing", exc)
    else:
        logger.warning("系统代理不可达，跳过 DuckDuckGo 直接走 Bing")
    try:
        return _search_bing(query, max_results)
    except Exception as exc:
        logger.warning("Bing 搜索失败（%s），尝试百度", exc)
    return _search_baidu(query, max_results)


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _search_bing(query: str, max_results: int) -> list[dict]:
    """Bing 国内站直连搜索（cn.bing.com，Clash 未启动时的首选降级源）。"""
    from ..network import make_httpx_client

    with make_httpx_client(timeout=8) as client:
        response = client.get(
            "https://cn.bing.com/search",
            params={"q": query},
            headers={"User-Agent": _BROWSER_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        response.raise_for_status()
        page_html = response.text
    results = _parse_bing_html(page_html, max_results)
    if not results:
        raise RuntimeError("Bing 搜索未解析到结果（页面结构变更或请求被拦截）")
    return results


def _decode_bing_url(href: str) -> str:
    """解开 Bing 跳转链（ck/a?...&u=a1<base64url> 里的真实 URL）；非跳转链原样返回。"""
    import base64
    import re

    match = re.search(r"[?&]u=a1([A-Za-z0-9_\-%]+)", href)
    if not match:
        return href
    token = match.group(1).split("%")[0]  # 截掉可能被 URL 编码的 "=" padding
    try:
        return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode(
            "utf-8", "replace"
        )
    except Exception:
        return href


def _parse_bing_html(page_html: str, max_results: int) -> list[dict]:
    """解析 Bing 结果页：li.b_algo 块内 h2>a 标题与链接，摘要取块内首个 <p>。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page_html, "html.parser")
    results: list[dict] = []
    seen: set[str] = set()
    for block in soup.select("li.b_algo"):
        link = block.select_one("h2 a")
        if link is None:
            continue
        title = link.get_text(strip=True)
        url = _decode_bing_url(link.get("href") or "").strip()
        if not title or not url.startswith("http") or url in seen:
            continue
        paragraph = block.find("p")
        snippet = paragraph.get_text(strip=True) if paragraph else ""
        seen.add(url)
        results.append(
            {
                "title": _truncate(title, 150),
                "url": url,
                "snippet": _truncate(snippet, 350),
            }
        )
        if len(results) >= max_results:
            break
    return results


def _search_baidu(query: str, max_results: int) -> list[dict]:
    """百度网页搜索（国内可直连；对无 cookie 自动化请求偶发图形验证，放末级）。"""
    from ..network import make_httpx_client

    with make_httpx_client(timeout=10) as client:
        response = client.get(
            "https://www.baidu.com/s",
            params={"wd": query, "rn": max(10, max_results * 2)},
            headers={"User-Agent": _BROWSER_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            follow_redirects=False,
        )
        if response.status_code // 100 == 3:
            # 302 → wappass.baidu.com 图形验证（无 cookie 自动化请求偶发）
            raise RuntimeError("百度搜索触发安全验证（302），请稍后重试或启动 Clash")
        response.raise_for_status()
        page_html = response.text
    results = _parse_baidu_html(page_html, max_results)
    if not results:
        raise RuntimeError(
            "百度搜索未解析到结果（可能触发安全验证或页面结构变更），"
            "可启动 Clash 后走 DuckDuckGo"
        )
    return results


def _parse_baidu_html(page_html: str, max_results: int) -> list[dict]:
    """解析百度结果页：结果块带 mu 属性（真实目标 URL，无需解析 link 跳转）。

    标题取块内首个 <h3>；摘要按代际依次尝试 c-abstract（老版）与
    summary-text_*（新版 cosc 卡片，类名带构建哈希）；部分卡片没有摘要取空串。
    fakeurl.baidu.com 是学术频道等卡片的占位链接，跳过。
    """
    import re

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page_html, "html.parser")
    results: list[dict] = []
    seen: set[str] = set()
    for block in soup.find_all("div", class_="c-container"):
        classes = block.get("class") or []
        if not any(c == "result" or c.startswith("result-") for c in classes):
            continue
        url = (block.get("mu") or "").strip()
        if not url.startswith("http") or url in seen or "fakeurl.baidu.com" in url:
            continue
        h3 = block.find("h3")
        if h3 is None:
            continue
        title = h3.get_text(strip=True)
        if not title:
            continue
        abstract = (
            block.find(class_="c-abstract")
            or block.find(class_=re.compile(r"summary-text_"))
            or block.find(class_=re.compile(r"content-right_"))
        )
        snippet = abstract.get_text(strip=True) if abstract else ""
        seen.add(url)
        results.append(
            {
                "title": _truncate(title, 150),
                "url": url,
                "snippet": _truncate(snippet, 350),
            }
        )
        if len(results) >= max_results:
            break
    return results


def _search_tavily(query: str, api_key: str, max_results: int) -> list[dict]:
    if not api_key:
        raise RuntimeError("未配置 TAVILY_API_KEY")
    # 走代理回退直连：系统代理（Clash 7890）未启动时不至于 WinError 10061
    from ..network import make_httpx_client

    response = make_httpx_client(timeout=30).post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        },
    )
    response.raise_for_status()
    data = response.json()
    return [
        {
            "title": _truncate(item.get("title", ""), 150),
            "url": item.get("url", ""),
            "snippet": _truncate(item.get("content", ""), 350),
        }
        for item in data.get("results", [])[:max_results]
    ]


def _search_searxng(
    query: str,
    base_url: str,
    max_results: int,
    engines: str = "",
) -> list[dict]:
    """SearXNG 自托管元搜索（JSON API，无限量免 key）。

    请求：GET {base_url}/search?q=...&format=json[&engines=a,b]
    响应：{results: [{title, url, content, ...}], unresponsive_engines: [...]}
    非 200 / 实例未启动会抛错。

    engines 逗号分隔指定引擎（如 "bing,baidu,sogou"），留空用实例默认。
    国内网络建议指定可直连/经代理稳定的引擎，避免 google 系超时拉低质量。

    403 常见原因（给出可操作提示）：
    1. 实例默认只开 html 格式，需在 settings.yml 加 search.formats: [html, json]；
    2. server.limiter 默认拦无转发头的请求（本客户端已带 X-Forwarded-For 头，
       仍 403 则需在 settings.yml 设 server.limiter: false）；
    3. 容器内访问 Google/Bing 需要走宿主机代理（Clash 7890）：
       docker run -e HTTP_PROXY=http://host.docker.internal:7890 \\
                  -e HTTPS_PROXY=http://host.docker.internal:7890 ...
    """
    import httpx
    from urllib.parse import urlencode

    base = (base_url or "").rstrip("/")
    if not base:
        raise RuntimeError("未配置 SearXNG 实例地址（SEARXNG_BASE_URL）")
    params: dict = {
        "q": query,
        "format": "json",
        "language": "zh-CN",
        "categories": "general",
    }
    engine_list = [
        e.strip() for e in (engines or "").split(",") if e.strip()
    ]
    if engine_list:
        params["engines"] = ",".join(engine_list)
    # 带转发头：SearXNG 的 limiter（botdetection）对无 X-Forwarded-For /
    # X-Real-IP 的请求直接 403（日志：X-Forwarded-For nor X-Real-IP header
    # is set!）。本机自托管场景下带 127.0.0.1 头即可放行。
    response = httpx.get(
        f"{base}/search?{urlencode(params)}",
        timeout=30,
        headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"},
    )
    if response.status_code == 403:
        raise RuntimeError(
            "SearXNG 返回 403 Forbidden。三步配置："
            "① settings.yml 开 JSON：search.formats: [html, json]；"
            "② 关闭限流器：server.limiter: false（默认会拦无转发头的请求）；"
            "③ 容器访问上游引擎走宿主机代理（Docker 加 "
            "-e HTTP_PROXY=http://host.docker.internal:7890 -e HTTPS_PROXY=...）。"
            "改完重启容器即可。"
        )
    response.raise_for_status()
    data = response.json()
    results = [
        {
            "title": _truncate(item.get("title", ""), 150),
            "url": item.get("url", ""),
            "snippet": _truncate(
                item.get("content") or item.get("snippet") or "", 350
            ),
        }
        for item in data.get("results", [])[:max_results]
    ]
    if not results:
        # 空结果：带上失败引擎诊断（如 unresponsive_engines），便于排查
        dead = data.get("unresponsive_engines") or []
        if dead:
            dead_summary = "；".join(
                f"{name}（{reason}）" for name, reason in dead[:5]
            )
            raise RuntimeError(
                f"SearXNG 未返回结果，上游引擎全部不可用：{dead_summary}。"
                "检查容器代理（host.docker.internal:7890）是否可用、"
                "或在设置里指定引擎（如 searxng_engines=bing,baidu,sogou）。"
            )
    return results


# ---------------- LangChain 工具工厂 ----------------

def make_knowledge_base_tool(
    rag_service,
    counter: list[int],
    crag_enabled: bool,
    crag_min_score: float,
    web_provider: str,
    tavily_api_key: str,
    web_max_results: int,
    allow_web_fallback: bool = True,
    searxng_base_url: str = "",
    searxng_engines: str = "",
) -> BaseTool:
    """知识库检索工具。

    内置 CRAG 兜底：仅当用户本轮开启联网（allow_web_fallback）且
    知识库相关度不足时，才补充联网结果，避免静默拖慢知识库模式。
    """
    retriever = KnowledgeBaseRetriever(retrieve_fn=rag_service.retrieve)

    @tool
    def knowledge_base_search(query: str) -> dict:
        """在用户的个人知识库（书籍/笔记/资料，清单见系统提示）中检索。

何时用：问题涉及书籍/文档内容；不确定库里有没有某本书时（先查再决定是否联网）。
何时不用：纯常识问答、闲聊、与文档无关的写作或计算——直接回答，不要空跑检索。
query 写成明确的检索意图（书名 + 主题），别整句照搬原话；查一个点调一次，
不要在一次 query 里塞多个不相关问题。返回文本片段与来源（带 index，引用时用 [n] 标注）。"""
        # GPU 锁只覆盖检索段（embedding/rerank），生成阶段不占用，避免检索排队
        rag_service.acquire_gpu()
        try:
            result = execute_knowledge_search(query, retriever, counter)
        finally:
            rag_service.release_gpu()
        best = max(
            (float(r.get("score") or 0) for r in result.get("results", [])),
            default=0.0,
        )
        if (
            crag_enabled
            and allow_web_fallback
            and web_provider != "off"
            and (not result.get("results") or best < crag_min_score)
        ):
            web = execute_web_search(
                query,
                web_provider,
                tavily_api_key,
                web_max_results,
                searxng_base_url,
                searxng_engines,
            )
            fallback = web.get("results", [])
            for item in fallback:
                counter[0] += 1
                item["index"] = counter[0]
                item["type"] = "web"
            result["fallback_web"] = fallback
            result["summary"] = "知识库未检索到足够相关的内容，已补充联网搜索结果"
        return result

    return knowledge_base_search


def make_add_document_tool(rag_service, settings) -> BaseTool:
    """把一段文本写入用户知识库并更新索引（敏感操作，走人工确认）。"""

    @tool
    def add_document(filename: str, content: str, source_note: str = "") -> dict:
        """把一段文本保存为知识库新文档（写文件并更新索引）。敏感操作，需用户确认。
        filename 只能是不含路径的纯文件名，支持 .txt/.md/.csv/.json；source_note 可选。
        """
        from pathlib import Path

        from ..tools_extra import _atomic_write

        name = (filename or "").strip()
        if (
            not name
            or "/" in name
            or "\\" in name
            or ".." in name
            or Path(name).name != name
        ):
            return {
                "error": "文件名不合法：只允许纯文件名（如 notes.md）",
                "summary": "写入知识库失败：文件名不合法",
            }
        ext = Path(name).suffix.lower()
        if ext not in (".txt", ".md", ".csv", ".json"):
            return {
                "error": f"暂不支持该格式：{ext}",
                "summary": "写入知识库失败：格式不支持",
            }
        try:
            data_dir = Path(rag_service.settings.data_dir).resolve()
            data_dir.mkdir(parents=True, exist_ok=True)
            target = data_dir / name
            text = str(content or "")
            if source_note.strip():
                text = f"{text}\n\n---\n来源说明：{source_note.strip()}"
            _atomic_write(target, text)
            rag_service.ensure_index()  # 增量索引：只嵌入新增/变化的文件
        except Exception as exc:
            return {"error": str(exc), "summary": "写入知识库失败"}
        return {
            "summary": f"已写入知识库文档 {name} 并更新索引",
            "filename": name,
        }

    return add_document


def make_web_search_tool(
    provider: str,
    tavily_api_key: str,
    max_results: int,
    counter: list[int],
    searxng_base_url: str = "",
    searxng_engines: str = "",
) -> BaseTool:
    """联网搜索工具：provider 可插拔（duckduckgo / tavily / searxng）。"""

    @tool
    def web_search(query: str) -> dict:
        """联网搜索公开信息（新闻/百科/实时数据）。涉及时事、网络信息，或知识库覆盖不足时调用；库内文档问题优先 knowledge_base_search。返回标题、链接、摘要（带 index，引用时用 [n] 标注）。"""
        result = execute_web_search(
            query,
            provider,
            tavily_api_key,
            max_results,
            searxng_base_url,
            searxng_engines,
        )
        for item in result.get("results", []):
            counter[0] += 1
            item["index"] = counter[0]
            item["type"] = "web"
        return result

    return web_search


def make_vision_tool(vision) -> BaseTool:
    """识图工具：主模型没有视觉能力时，按需调用 SenseNova 识别图片内容。

    为什么值得做一个"工具"而不是只靠自动识图：
    - 自动识图只覆盖"用户当前上传的图片"；
    - 工具方式让 Agent 在对话中按需对任意 base64 图片做 OCR / 图表解读。
    """

    @tool
    def image_to_text(image_base64: str, question: str = "请详细描述这张图片的内容") -> dict:
        """识别图片内容（OCR/图表/照片理解）。参数 image_base64 为 base64 字符串（可含 data:image/...;base64, 前缀）。"""
        try:
            description = vision.describe_images(
                [image_base64],
                question=question,
            )
            return {"summary": f"图片识别结果：{description}", "description": description}
        except Exception as exc:
            return {"summary": f"图片识别失败：{exc}", "error": str(exc)}

    return image_to_text


SKILL_VIEW_TOOL_NAME = "skill_view"

_SKILL_FILE_MAX_BYTES = 256 * 1024
_MAX_SECTION_DIGEST = 60
_SECTION_SUMMARY_CHARS = 160


def _match_skill(skills: list[dict], name: str) -> dict | None:
    """按技能名（或目录名）匹配，大小写不敏感。"""
    target = (name or "").strip().lower()
    if not target:
        return None
    for skill in skills:
        if str(skill.get("name") or "").lower() == target:
            return skill
    for skill in skills:
        if str(skill.get("dir_name") or "").lower() == target:
            return skill
    return None


def _match_section(sections: list[dict], section: str) -> dict | None:
    target = (section or "").strip().lower()
    if not target:
        return None
    for item in sections:
        if item["heading"].strip().lower() == target:
            return item
    for item in sections:
        if target in item["heading"].strip().lower():
            return item
    return None


def _safe_skill_file(skill_dir: Path, file_path: str) -> Path:
    """把技能目录内的相对路径解析为绝对路径；越界/绝对路径/穿越一律拒绝。"""
    rel = (file_path or "").strip().replace("\\", "/")
    if not rel:
        raise ValueError("file_path 不能为空")
    if rel.startswith("/") or re.match(r"^[a-zA-Z]:", rel):
        raise ValueError("只允许技能目录内的相对路径（不接受绝对路径或盘符）")
    if any(part in ("", "..") for part in rel.split("/")):
        raise ValueError("路径不合法（不允许 .. 或空路径段）")
    root = skill_dir.resolve()
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("路径超出技能目录")
    if not candidate.is_file():
        raise ValueError("文件不存在")
    return candidate


def _section_digest(sections: list[dict]) -> tuple[list[dict], bool]:
    """章节目录瘦身：最多 60 条、每条摘要 160 字，避免目录本身撑爆上下文。"""
    digest = [
        {
            "heading": s.get("heading", ""),
            "summary": (s.get("summary") or "")[:_SECTION_SUMMARY_CHARS],
        }
        for s in sections[:_MAX_SECTION_DIGEST]
    ]
    return digest, len(sections) > _MAX_SECTION_DIGEST


def _skill_payload(entry: dict, content: str, sanitize, full_max: int, extra=None) -> dict:
    body = sanitize(content, full_max) if content else ""
    payload = {
        "ok": True,
        "name": entry.get("name"),
        "source": entry.get("source"),
        "category": entry.get("category"),
        "group": entry.get("group"),
        "path": entry.get("path"),
        "skill_dir": entry.get("dir_path"),
        "linked_files": entry.get("linked_files") or {},
        "content": body,
        "content_chars": len(content),
        "truncated": False,
        "summary": f"技能 {entry.get('name')}（{len(body)} 字符）",
        "usage_hint": "附件可用 skill_view(name, file_path='references/...') 读取。",
    }
    if extra:
        payload.update(extra)
    return payload


def make_skill_view_tool(
    rag_service,
    enabled_ids: set[str] | None = None,
    *,
    conv_id: int | None = None,
    full_max_chars: int = 24000,
    dedup: bool = True,
    usage: bool = True,
) -> BaseTool:
    """技能加载工具：按名称取回 SKILL.md 全文、章节或附件。

    替代旧的向量 top-k 检索入口——L0 索引已把全部启用技能名交给模型，
    这里按准确名称做渐进披露（L1 全文/分节、L2 附件）。同一轮重复查看
    未变化的内容返回短 stub，避免重复注入撑爆上下文。
    """
    seen: dict[tuple[str, str, str], str] = {}

    @tool
    def skill_view(name: str, file_path: str = "", section: str = "") -> dict:
        """查看一个已启用技能（Skills）的完整内容或附件。
        name 必须是系统提示词技能目录中的技能名；任务与某技能部分相关就应调用。
        file_path 可选：技能目录内的相对路径（如 references/api.md），用于读附件。
        section 可选：按章节标题读取大技能的一部分（如 "When to Use"）。"""
        try:
            from ..skills import sanitize_skill_text, scan_skills, skill_sections

            skills = [
                s
                for s in scan_skills()
                if enabled_ids is None or s["name"] in enabled_ids
            ]
            entry = _match_skill(skills, name)
            if entry is None:
                return {
                    "ok": False,
                    "error": f"未找到已启用技能「{name}」",
                    "available_names": [s["name"] for s in skills][:50],
                }
            skill_md = Path(str(entry.get("path") or ""))
            skill_dir = Path(str(entry.get("dir_path") or skill_md.parent))

            if file_path:
                target = _safe_skill_file(skill_dir, file_path)
                size = target.stat().st_size
                if size > _SKILL_FILE_MAX_BYTES:
                    return {
                        "ok": False,
                        "error": (
                            f"附件过大（{size} 字节，上限 {_SKILL_FILE_MAX_BYTES}），"
                            "请改读 references/ 下更小的文件"
                        ),
                    }
                text = target.read_text(encoding="utf-8", errors="ignore")
                return _skill_payload(
                    entry,
                    text,
                    sanitize_skill_text,
                    full_max_chars,
                    extra={"file_path": file_path},
                )

            text = skill_md.read_text(encoding="utf-8", errors="ignore")
            sections = skill_sections(str(skill_md))

            if section:
                hit = _match_section(sections, section)
                if hit is None:
                    return {
                        "ok": False,
                        "error": f"未找到章节「{section}」",
                        "sections": _section_digest(sections)[0],
                    }
                return _skill_payload(
                    entry,
                    text[hit["start"] : hit["end"]],
                    sanitize_skill_text,
                    full_max_chars,
                    extra={"section": hit["heading"]},
                )

            if len(text) <= full_max_chars:
                payload = _skill_payload(
                    entry, text, sanitize_skill_text, full_max_chars
                )
            else:
                digest, digest_truncated = _section_digest(sections)
                payload = _skill_payload(
                    entry,
                    "",
                    sanitize_skill_text,
                    full_max_chars,
                    extra={
                        "truncated": True,
                        "content_chars": len(text),
                        "sections": digest,
                        "sections_truncated": digest_truncated,
                        "summary": (
                            f"技能 {entry.get('name')} 较大（{len(text)} 字符），"
                            f"已返回章节目录（{len(digest)} 节），用 section 读取指定章节"
                        ),
                        "usage_hint": (
                            "技能较大，已返回章节目录；用 "
                            "skill_view(name, section='<标题>') 读取指定章节。"
                        ),
                    },
                )
                if not sections:
                    payload["content"] = sanitize_skill_text(text, full_max_chars)

            key = (str(entry.get("name")), file_path, section)
            if dedup and seen.get(key) == payload.get("content"):
                return {
                    "ok": True,
                    "name": entry.get("name"),
                    "path": str(skill_md),
                    "skill_dir": str(skill_dir),
                    "deduplicated": True,
                    "summary": f"技能 {entry.get('name')} 内容本轮已返回且未变化，未重复注入。",
                    "usage_hint": "如需重读请换 section/file_path，或直接使用已加载内容。",
                }
            seen[key] = payload.get("content") or ""

            if usage:
                try:
                    from ..skills import record_view

                    record_view(str(entry.get("name")))
                except Exception:
                    pass
            return payload
        except Exception as exc:
            return {"ok": False, "error": f"技能加载失败：{exc}"}

    return skill_view


# ---------------- 技能创作（把重复流程沉淀为可复用 SKILL.md）----------------

WRITE_SKILL_TOOL_NAME = "write_skill"


class _WriteSkillArgs(BaseModel):
    name: str = Field(
        description="技能名称（小写连字符，如 book-topic-compare）；同名技能会被覆盖"
    )
    description: str = Field(
        description="一句话描述技能用途（将写入 SKILL.md frontmatter 的 description）"
    )
    content: str = Field(
        description=(
            "SKILL.md 正文（不含 frontmatter）。"
            "用 markdown 写：何时用、前置条件、步骤、注意事项。"
            "正文开头会自动加上 frontmatter，你只需写 body。"
        )
    )
    category: str = Field(
        default="other",
        description="分类目录名（如 research/writing/coding/other），默认 other",
    )


def make_write_skill_tool(settings=None) -> BaseTool:
    """技能创作工具：把重复执行的流程沉淀成 SKILL.md，下次 skill_view 即可取回。

    写入自创技能库 data/agent_skills/<category>/<name>/SKILL.md（与 Hermes
    个人技能层隔离；设置 → 技能 → 自创 里可启用 / 共享给 Hermes 使用）。
    写完 force 刷新扫描缓存使新技能立即可见。属敏感操作（写文件），走 HITL 人工确认。
    """

    @tool
    def write_skill(
        name: str,
        description: str,
        content: str,
        category: str = "other",
    ) -> dict:
        """把一个可复用的任务流程写成技能文件（SKILL.md），下次同类任务用 skill_view 取回。
        适合：刚走通、以后会重复的多步流程；不适合：一次性任务与 1-2 步操作。
        name 小写连字符，description 一句话，content 为不含 frontmatter 的正文，category 默认 other。
        正文建议包含 When to Use / Prerequisites / Procedure / Pitfalls / Verification 章节。"""
        try:
            import os
            import re
            from datetime import date

            # 名称规范化：小写、连字符、只保留字母数字
            clean_name = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())
            clean_name = re.sub(r"-+", "-", clean_name).strip("-")
            if not clean_name or len(clean_name) < 2:
                return {
                    "summary": "技能名称无效（至少 2 个字母数字字符）",
                    "error": "invalid name",
                }
            clean_cat = re.sub(
                r"[^a-z0-9-]", "-", (category or "other").lower().strip()
            ).strip("-") or "other"

            # 自创技能落点：data/agent_skills（与 Hermes 个人层隔离，按需共享）
            from ..skills import AGENT_SKILLS_DIR

            skills_root = str(AGENT_SKILLS_DIR)
            skill_dir = os.path.join(skills_root, clean_cat, clean_name)
            skill_md_path = os.path.join(skill_dir, "SKILL.md")

            # 检查是否已存在（覆盖前告知）
            existed = os.path.exists(skill_md_path)

            # 组装 frontmatter + body
            desc_clean = description.strip().replace('"', "'")[:200]
            frontmatter = (
                "---\n"
                f"name: {clean_name}\n"
                f'description: "{desc_clean}"\n'
                "origin: agent\n"
                f"created_at: {date.today().isoformat()}\n"
                "---\n\n"
            )
            full_content = frontmatter + content.strip() + "\n"

            os.makedirs(skill_dir, exist_ok=True)
            with open(skill_md_path, "w", encoding="utf-8") as f:
                f.write(full_content)

            # force 刷新 scan_skills 缓存，使新技能在下一次 skill_view 时可见
            try:
                from ..skills import scan_skills

                scan_skills(force=True)
            except Exception:
                pass  # 缓存刷新失败不影响写入成功

            if settings is None or bool(
                effective_or(settings, "skills_usage_tracking", True)
            ):
                try:
                    from ..skills import record_patch

                    record_patch(clean_name)
                except Exception:
                    pass

            return {
                "summary": (
                    f"技能 {clean_name} 已{'覆盖' if existed else '创建'}"
                    f"（自创技能库 {clean_cat}/{clean_name}/SKILL.md，"
                    f"{len(full_content)} 字符，当前为待启用状态）。"
                    "在 设置 → 技能 → 自创 里可启用（启用后 skill_view 才能查到）；"
                    "如需给 Hermes 使用，可在同一处点「共享」。"
                ),
                "name": clean_name,
                "path": skill_md_path,
                "category": clean_cat,
                "overwritten": existed,
            }
        except Exception as exc:
            return {"summary": f"技能创建失败：{exc}", "error": str(exc)}

    return write_skill


def build_tools(
    rag_service,
    use_web_search: bool,
    use_knowledge_base: bool,
    provider: str,
    tavily_api_key: str,
    max_results: int,
    crag_enabled: bool = True,
    crag_min_score: float = 0.45,
    vision=None,
    skills_enabled: bool = True,
    skill_enabled_ids: set[str] | None = None,
    searxng_base_url: str = "",
    searxng_engines: str = "",
    conv_id: int | None = None,
) -> list[BaseTool]:
    """根据前端开关组装工具列表（都不开则返回空列表 = 纯对话）。"""
    tools: list[BaseTool] = []
    counter: list[int] = [0]  # 本次对话的全局引用编号器
    if use_knowledge_base:
        tools.append(
            make_knowledge_base_tool(
                rag_service,
                counter,
                crag_enabled,
                crag_min_score,
                provider,
                tavily_api_key,
                max_results,
                allow_web_fallback=use_web_search,
                searxng_base_url=searxng_base_url,
                searxng_engines=searxng_engines,
            )
        )
        tools.append(make_add_document_tool(rag_service, rag_service.settings))
    if use_web_search:
        tools.append(
            make_web_search_tool(
                provider,
                tavily_api_key,
                max_results,
                counter,
                searxng_base_url,
                searxng_engines,
            )
        )
    if vision is not None and getattr(vision, "configured", False):
        tools.append(make_vision_tool(vision))
    if skills_enabled:
        try:
            from ..skills import scan_skills

            if scan_skills():
                settings = getattr(rag_service, "settings", None)
                tools.append(
                    make_skill_view_tool(
                        rag_service,
                        enabled_ids=skill_enabled_ids,
                        conv_id=conv_id,
                        full_max_chars=int(
                            effective_or(
                                settings, "skills_full_view_max_chars", 24000
                            )
                            or 24000
                        ),
                        dedup=bool(
                            effective_or(settings, "skills_view_dedup", True)
                        ),
                        usage=bool(
                            effective_or(settings, "skills_usage_tracking", True)
                        ),
                    )
                )
                # 技能创作工具：让 agent 把重复流程沉淀为 SKILL.md（敏感操作走 HITL）
                tools.append(make_write_skill_tool(settings))
        except Exception as exc:
            logger.warning("技能工具注册失败：%s", exc)
    return tools


# ---------------- 计划模式控制工具（模型自主进入 + 结构化提交）----------------

PLAN_ENTER_TOOL_NAME = "enter_plan_mode"
PLAN_EXIT_TOOL_NAME = "exit_plan_mode"

PLAN_MODE_SYSTEM_HINT = (
    "# 计划模式（只读）\n"
    "你已进入计划模式：只能读取/检索/搜索来了解现状，"
    "写文件、编辑、删除、执行命令都会被系统拦截。\n"
    "请先用只读手段把现状摸清（必要时用 dispatch_subtasks 并行探索），"
    "然后输出一份可执行的完整计划（步骤、每步要动什么、预期影响、需要用户确认的点），"
    "并调用 exit_plan_mode 提交结构化步骤。用户确认后，系统会按同一份计划发起执行。"
)

_ENTER_PLAN_DESCRIPTION = (
    "进入计划模式（只读）：先探索、出方案、等用户确认再动手。\n"
    "适合：写文件/执行命令的多步任务，方案不确定，或改动影响面大、不可逆。\n"
    "不适合：问答检索、一两步的小改动（白多一轮确认）。\n"
    "进入后写文件/命令被拦截；整理好计划用 exit_plan_mode 提交，"
    "用户确认后按同一计划执行。每个会话只允许进入一次。"
)

_EXIT_PLAN_DESCRIPTION = (
    "提交结构化执行计划并结束本轮，等待用户确认。\n"
    "steps 必须可执行（动词开头、含具体对象/路径）；用户会看到并可修改，"
    "确认后按此清单逐步执行（并成为任务列表）。"
)


class _ExitPlanArgs(BaseModel):
    steps: list[str] = Field(
        description="3~5 步可执行计划；每步动词开头、含具体对象或文件路径"
    )
    summary: str = Field(
        default="", description="一句话说明计划目标与整体影响（可选）"
    )


def make_enter_plan_mode_tool() -> BaseTool:
    """计划模式入口（无副作用：真正改状态在 tools 节点）。"""

    def _invoke(reason: str = "") -> dict:
        return {"summary": "已进入计划模式（只读）", "reason": str(reason)[:200]}

    return StructuredTool.from_function(
        func=_invoke,
        name=PLAN_ENTER_TOOL_NAME,
        description=_ENTER_PLAN_DESCRIPTION,
        args_schema=None,
    )


def make_exit_plan_mode_tool() -> BaseTool:
    """计划模式出口：提交结构化步骤（由 tools 节点转成等待确认状态）。"""

    def _invoke(steps: list[str], summary: str = "") -> dict:
        clean = [str(s).strip() for s in (steps or []) if str(s).strip()][:8]
        return {
            "summary": f"计划已提交（{len(clean)} 步），等待用户确认",
            "steps": clean,
        }

    return StructuredTool.from_function(
        func=_invoke,
        name=PLAN_EXIT_TOOL_NAME,
        description=_EXIT_PLAN_DESCRIPTION,
        args_schema=_ExitPlanArgs,
    )


# ---------------- 结果解析 ----------------

def parse_tool_message(message: ToolMessage) -> dict:
    """ToolNode 会把 dict 输出序列化成 JSON 字符串，这里还原成 dict。"""
    content = message.content
    if isinstance(content, str):
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"summary": content, "results": []}


def extract_sources(result: dict) -> list[dict]:
    """从工具结果里抽取引用来源（知识库片段 + 联网条目，供前端展示）。"""
    sources: list[dict] = []
    items = list(result.get("results", [])) + list(result.get("fallback_web", []))
    for item in items:
        if item.get("type") == "web":
            sources.append(
                {
                    "index": item.get("index"),
                    "type": "web",
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "content": item.get("snippet", "") or item.get("content", ""),
                }
            )
        elif item.get("source") or item.get("content"):
            sources.append(
                {
                    "index": item.get("index"),
                    "type": "kb",
                    "content": item.get("content", ""),
                    "score": item.get("score"),
                    "source": item.get("source"),
                    "page": item.get("page"),
                    "relative_path": item.get("relative_path"),
                }
            )
    return sources
