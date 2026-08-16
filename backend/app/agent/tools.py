"""Agent 工具（LangChain @tool 实现）：知识库检索 + 联网搜索。

设计说明：
- 用 langchain_core.tools.tool 装饰器定义工具，交给 Agent 的 ReAct 循环托管；
- 知识库检索把现有 RAG 管道包装成 LangChain Retriever（KnowledgeBaseRetriever）；
- 联网搜索支持 provider 可插拔：duckduckgo（免 key）/ tavily（需 key）/ off。
"""

from __future__ import annotations

import json
import logging

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool

from ..rag.retriever import KnowledgeBaseRetriever

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


def execute_web_search(query: str, provider: str, api_key: str, max_results: int) -> dict:
    """联网搜索：provider = duckduckgo | tavily | off。"""
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
        else:
            results = _search_duckduckgo(query, max_results)
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


def _search_tavily(query: str, api_key: str, max_results: int) -> list[dict]:
    if not api_key:
        raise RuntimeError("未配置 TAVILY_API_KEY")
    import httpx

    response = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        },
        timeout=30,
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
) -> BaseTool:
    """知识库检索工具。

    内置 CRAG 兜底：仅当用户本轮开启联网（allow_web_fallback）且
    知识库相关度不足时，才补充联网结果，避免静默拖慢知识库模式。
    """
    retriever = KnowledgeBaseRetriever(retrieve_fn=rag_service.retrieve)

    @tool
    def knowledge_base_search(query: str) -> dict:
        """在用户的个人知识库中检索相关信息。知识库包含用户上传的全部文档（书籍、笔记、资料等），文档清单会列在系统提示中。当问题涉及任何书籍/文档内容时，或你不确定某本书是否在知识库中时，都应先调用本工具确认。返回最相关的文本片段及来源（每条带 index 引用编号）。"""
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
                query, web_provider, tavily_api_key, web_max_results
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
        """把一段文本保存为用户知识库的新文档（写入文件并更新索引）。
        属敏感操作，系统会请求用户确认；filename 只能是不含路径的纯文件名，
        支持 .txt/.md/.csv/.json；source_note 可选，用于记录来源说明。
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
) -> BaseTool:
    """联网搜索工具：provider 可插拔。"""

    @tool
    def web_search(query: str) -> dict:
        """联网搜索互联网上的公开信息（新闻、百科、实时数据等）。当问题涉及时事、网络信息，或知识库检索确认无法覆盖时调用；知识库已有文档相关的问题请优先使用 knowledge_base_search。返回标题、链接和摘要（每条带 index 引用编号，回答引用时用 [n] 标注）。"""
        result = execute_web_search(query, provider, tavily_api_key, max_results)
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
        """识别图片内容（OCR / 图表 / 照片理解）。当需要理解图片中的文字、表格或场景时调用。参数 image_base64 为图片的 base64 字符串（可含 data:image/...;base64, 前缀）。"""
        try:
            description = vision.describe_images(
                [image_base64],
                question=question,
            )
            return {"summary": f"图片识别结果：{description}", "description": description}
        except Exception as exc:
            return {"summary": f"图片识别失败：{exc}", "error": str(exc)}

    return image_to_text


def make_skill_lookup_tool(
    rag_service,
    enabled_ids: set[str] | None = None,
) -> BaseTool:
    """技能检索工具：按需取回本助手已启用的 Agent Skills 指令。"""

    @tool
    def skill_lookup(query: str) -> dict:
        """检索本机可复用的 Agent 技能（Skills）。当用户要求"用某个技能/方法处理任务"、
        或当前任务看起来属于某个专业流程（写作、设计、部署、研究等）时调用。
        返回匹配技能的名称、描述与 SKILL.md 摘要，供按步骤执行。"""
        try:
            from ..skills import search_skills
            from ..skills import skill_structure

            hits = search_skills(
                query,
                embeddings=rag_service.embeddings,
                top_k=3,
                enabled_ids=enabled_ids,
            )
            if not hits:
                return {"summary": "未找到匹配的技能", "skills": []}
            from ..skills import sanitize_skill_text

            items = [
                {
                    "name": h["name"],
                    "source": h["source"],
                    "description": sanitize_skill_text(h["description"], 300),
                    "instructions": sanitize_skill_text(h["excerpt"], 1200),
                    "structure": [
                        {
                            "heading": s["heading"],
                            "summary": sanitize_skill_text(s["summary"], 300),
                        }
                        for s in skill_structure(h["path"])
                    ],
                }
                for h in hits
            ]
            return {
                "summary": (
                    f"找到 {len(items)} 个匹配技能："
                    f"{'、'.join(i['name'] for i in items)}"
                    "（structure 中为分段摘要：使用时机/前置条件/步骤）"
                ),
                "skills": items,
            }
        except Exception as exc:
            return {"summary": f"技能检索失败：{exc}", "skills": []}

    return skill_lookup


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
            )
        )
        tools.append(make_add_document_tool(rag_service, rag_service.settings))
    if use_web_search:
        tools.append(
            make_web_search_tool(provider, tavily_api_key, max_results, counter)
        )
    if vision is not None and getattr(vision, "configured", False):
        tools.append(make_vision_tool(vision))
    if skills_enabled:
        try:
            from ..skills import scan_skills

            if scan_skills():
                tools.append(
                    make_skill_lookup_tool(
                        rag_service,
                        enabled_ids=skill_enabled_ids,
                    )
                )
        except Exception as exc:
            logger.warning("技能工具注册失败：%s", exc)
    return tools


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
