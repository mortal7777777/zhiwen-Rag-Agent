"""知识库检索工具（CRAG 联网兜底）与写文档工具。"""

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
from .web_search import execute_web_search

logger = logging.getLogger(__name__)


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
    # 条数由检索层控制（rerank_top_k / BROAD_CONTEXT_BUDGET / 每书配额），
    # 工具层不再二次腰斩；900 对齐 parent_max_chunk_size，保留完整段落。
    for doc in docs:
        counter[0] += 1
        results.append(
            {
                "index": counter[0],
                "type": "kb",
                "content": _truncate(doc.page_content, 900),
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
                "pages": doc.metadata.get("pages"),
                "relative_path": doc.metadata.get("relative_path"),
                # P1：图片块引用（PP-Structure 裁剪的脑图/图表/范例），前端来源卡片展示
                "images": [
                    {
                        "id": img.get("id"),
                        "path": img.get("path"),
                        "bbox": img.get("bbox"),
                    }
                    for img in (doc.metadata.get("image_refs") or [])[:6]
                ],
            }
        )
    return {
        "query": query,
        "results": results,
        "summary": f"知识库检索到 {len(results)} 条相关内容",
    }


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
    scope_paths: list[str] | None = None,
) -> BaseTool:
    """知识库检索工具。

    内置 CRAG 兜底：仅当用户本轮开启联网（allow_web_fallback）且
    知识库相关度不足时，才补充联网结果，避免静默拖慢知识库模式。
    scope_paths：用户限定的检索范围（单文档/整本书的多个文件）。
    """
    if scope_paths:
        def _scoped_retrieve(query: str):
            return rag_service.retrieve(query, scope_paths=scope_paths)

        retriever = KnowledgeBaseRetriever(retrieve_fn=_scoped_retrieve)
    else:
        retriever = KnowledgeBaseRetriever(retrieve_fn=rag_service.retrieve)

    # description 显式传入：@tool 会把 docstring 原样复制进 args schema，
    # 工具面预算（test_tools_contract）下属于纯重复计费。
    @tool(
        description=(
            "在用户的个人知识库（书籍/笔记/资料，清单见系统提示）中检索。\n\n"
            "何时用：问题涉及书籍/文档内容；不确定库里有没有某本书时（先查再决定是否联网）。\n"
            "何时不用：纯常识问答、闲聊、与文档无关的写作或计算——直接回答，不要空跑检索。\n"
            "query 写成明确的检索意图（书名 + 主题），别整句照搬原话；查一个点调一次，\n"
            "不要在一次 query 里塞多个不相关问题。返回文本片段与来源（带 index，引用时用 [n] 标注）。"
        )
    )
    def knowledge_base_search(query: str) -> dict:
        # GPU 锁只覆盖检索段（embedding/rerank），生成阶段不占用，避免检索排队
        rag_service.acquire_gpu()
        try:
            result = execute_knowledge_search(query, retriever, counter)
        finally:
            rag_service.release_gpu()
        # 图形/图表问题：让视觉模型先看图，文本结果注入后再交给主模型回答
        try:
            rag_service.enrich_result_with_vision(result, query)
        except Exception as exc:
            logger.warning("知识库视觉增强失败：%s", exc)
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

    @tool(
        description=(
            "把一段文本保存为知识库新文档（写文件并更新索引）。敏感操作，需用户确认。\n"
            "filename 只能是不含路径的纯文件名，支持 .txt/.md/.csv/.json；source_note 可选。"
        )
    )
    def add_document(filename: str, content: str, source_note: str = "") -> dict:
        from pathlib import Path

        from ...tools_extra import _atomic_write

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
