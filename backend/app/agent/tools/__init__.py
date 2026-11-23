"""Agent 工具集（按域拆分的包；保持原 `app.agent.tools` 导入路径）。

顺序即 bind_tools 的 tools 数组顺序（前缀缓存键），`build_tools` 保持原顺序。
"""

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
from .kb import execute_knowledge_search, make_add_document_tool, make_knowledge_base_tool
from .plan import (
    ASK_QUESTION_TOOL_NAME,
    PLAN_ENTER_TOOL_NAME,
    PLAN_EXIT_TOOL_NAME,
    PLAN_MODE_SYSTEM_HINT,
    _ASK_DESCRIPTION,
    _ENTER_PLAN_DESCRIPTION,
    _EXIT_PLAN_DESCRIPTION,
    _AskArgs,
    _ExitPlanArgs,
    make_ask_user_question_tool,
    make_enter_plan_mode_tool,
    make_exit_plan_mode_tool,
)
from .skill import (
    SKILL_VIEW_TOOL_NAME,
    WRITE_SKILL_TOOL_NAME,
    _MAX_SECTION_DIGEST,
    _SECTION_SUMMARY_CHARS,
    _SKILL_FILE_MAX_BYTES,
    _WriteSkillArgs,
    _match_section,
    _match_skill,
    _safe_skill_file,
    _section_digest,
    _skill_payload,
    make_skill_view_tool,
    make_write_skill_tool,
)
from .web_search import (
    _BROWSER_UA,
    _HIGH_CREDIBILITY_DOMAINS,
    _LOW_CREDIBILITY_DOMAINS,
    _MEDIUM_CREDIBILITY_DOMAINS,
    _credibility_for,
    _decode_bing_url,
    _parse_baidu_html,
    _parse_bing_html,
    _search_baidu,
    _search_bing,
    _search_duckduckgo,
    _search_searxng,
    _search_tavily,
    _search_with_fallback,
    execute_web_search,
    make_web_search_tool,
)

logger = logging.getLogger(__name__)


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
    kb_scope_paths: list[str] | None = None,
    counter: list[int] | None = None,
) -> list[BaseTool]:
    """根据前端开关组装工具列表（都不开则返回空列表 = 纯对话）。

    counter：全局引用编号器，必须由调用方传入 AgentState 里的同一个列表，
    否则主工具与 merge 会把子代理来源重编号到重复的 [n]。
    """
    tools: list[BaseTool] = []
    counter = counter if counter is not None else [0]
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
                scope_paths=kb_scope_paths,
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
            from ...skills import scan_skills

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
                    "pages": item.get("pages"),
                    "relative_path": item.get("relative_path"),
                    "images": item.get("images") or [],
                }
            )
    return sources
