"""联网搜索：可信度标注、多引擎降级链与 web_search 工具。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool, tool
from pydantic import BaseModel, Field

from ...rag.retriever import KnowledgeBaseRetriever
from ...runtime_config import effective_or
from .common import _truncate

logger = logging.getLogger(__name__)


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
    from ...network import proxy_reachable

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
    from ...network import make_httpx_client

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
    from ...network import make_httpx_client

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
    from ...network import make_httpx_client

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


def make_web_search_tool(
    provider: str,
    tavily_api_key: str,
    max_results: int,
    counter: list[int],
    searxng_base_url: str = "",
    searxng_engines: str = "",
) -> BaseTool:
    """联网搜索工具：provider 可插拔（duckduckgo / tavily / searxng）。"""

    @tool(
        description=(
            "联网搜索公开信息（新闻/百科/实时数据）。涉及时事、网络信息，或知识库覆盖不足时调用；"
            "库内文档问题优先 knowledge_base_search。"
            "返回标题、链接、摘要（带 index，引用时用 [n] 标注）。"
        )
    )
    def web_search(query: str) -> dict:
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
