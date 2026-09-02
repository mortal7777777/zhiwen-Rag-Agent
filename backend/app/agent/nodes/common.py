"""节点共享辅助：计划工具提示、项目任务判定、预算与失败上限、深度思考摘要生成（自 langgraph_agent.py 拆分）。"""

from __future__ import annotations

import logging


from ...runtime_config import effective

logger = logging.getLogger(__name__)
from ..state import AgentState

from langchain_core.messages import HumanMessage
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..langgraph_agent import LangGraphAgentService  # 仅类型注解

# ==== 函数体（原文）====
def _plan_hint(step: str) -> dict:
    """解析计划步骤的建议手段（工具提示），让 plan 真正指导执行。"""
    s = step
    tool_hint = ""
    if any(k in s for k in ("知识库", "检索文档", "文档", "书籍", "著作", "章节")):
        tool_hint = "knowledge_base_search"
    elif any(k in s for k in ("联网", "搜索", "新闻", "网络", "实时", "热点")):
        tool_hint = "web_search"
    elif any(
        k in s
        for k in (
            "文件夹",
            "文件",
            "写入",
            "编辑",
            "创建",
            "删除",
            "代码",
            "脚本",
            "运行",
            "测试",
            "项目",
        )
    ):
        tool_hint = "file_tool/bash"
    return {"step": s, "tool_hint": tool_hint}


_TASK_MODE_KEYWORDS = (
    "完成", "实现", "开发", "搭建", "重构", "改造", "整个项目", "端到端",
    "从零", "全部做完", "把这个项目", "做完整个",
)


def _is_project_task(settings, question: str, plan_steps: list[str]) -> bool:
    """识别项目级任务：关键词 / 计划步数较多 / 长问题且计划需要工具。"""
    if not effective(settings, "task_mode_detect"):
        return False
    q = question or ""
    if any(k in q for k in _TASK_MODE_KEYWORDS):
        return True
    steps = plan_steps or []
    if len(steps) >= 4:
        return True
    return len(q) >= 80 and any(
        _plan_hint(s).get("tool_hint") for s in steps
    )


def _iteration_limit(state: AgentState, settings) -> int:
    if state.get("task_mode"):
        return max(
            int(settings.agent_max_iterations),
            int(effective(settings, "agent_task_max_iterations") or 24),
        )
    return int(settings.agent_max_iterations)


def _failure_limit(state: AgentState, settings) -> int:
    if state.get("task_mode"):
        return max(
            int(getattr(settings, "agent_max_failures", 3)),
            int(effective(settings, "agent_task_max_failures") or 6),
        )
    return int(getattr(settings, "agent_max_failures", 3))


def _generate_reasoning_summary(
    service: "LangGraphAgentService",
    state: AgentState,
    runtime: dict,
) -> str:
    """生成"已深度思考"摘要：基于计划+工具轨迹，用低温模型写一段简短思考过程。"""
    trace_lines = "\n".join(
        f"- 调用 {t.get('name')}：{str(t.get('summary') or '')[:80]}"
        for t in (state.get("tool_trace") or [])[:4]
    )
    prompt = (
        "根据下面的执行过程，用第一人称写一段不超过 120 字的'思考摘要'，"
        "说明你如何分析问题、检索/搜索了什么、得到什么结论，以及为什么这样回答。"
        "只输出摘要本身，不要解释。\n\n"
        f"问题：{state['question'][:300]}\n"
        f"计划：{'；'.join(state.get('plan_steps') or [])}\n"
        f"工具过程：\n{trace_lines or '（无工具调用）'}"
    )
    try:
        resp = service.context._invoke([HumanMessage(content=prompt)])
        return (resp.content or "").strip()[:200]
    except Exception as exc:
        logger.warning("思考摘要生成失败：%s", exc)
        return ""


# ============================================================
# 节点：prepare
# ============================================================



_TASK_MODE_KEYWORDS = (
    "完成", "实现", "开发", "搭建", "重构", "改造", "整个项目", "端到端",
    "从零", "全部做完", "把这个项目", "做完整个",
)


