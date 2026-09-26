"""节点共享辅助：计划工具提示、项目任务判定、预算与失败上限、深度思考摘要生成（自 langgraph_agent.py 拆分）。"""

from __future__ import annotations

import logging


from ...llm_text import message_text
from ...runtime_config import effective

logger = logging.getLogger(__name__)
from ..state import AgentState

from langchain_core.messages import HumanMessage
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..langgraph_agent import LangGraphAgentService  # 仅类型注解

# ==== 函数体（原文）====
# 计划步骤的工具提示：只用于"这一步需不需要工具"（硬约束、预算、清单进度），
# **不再承担任何路由职责**——派发由主代理的显式声明决定（subagent.py）。
# 因此取值只需区分两类：
#   write = 有副作用的步骤（写/改文件、跑命令）→ 触发项目级预算；
#   tool  = 只读/检索类步骤（需要工具但无副作用）；
#   ""    = 纯推理步骤（收尾时视为已被最终回答覆盖）。
_PLAN_HINT_WRITE = "write"
_PLAN_HINT_TOOL = "tool"

_READ_KEYWORDS = (
    "知识库", "检索文档", "文档", "书籍", "著作", "章节",
    "联网", "搜索", "新闻", "网络", "实时", "热点",
)
_WRITE_KEYWORDS = (
    "文件夹", "文件", "写入", "编辑", "创建", "删除", "代码", "脚本",
    "运行", "测试", "项目", "生成", "保存", "导出",
)


def _plan_hint(step: str) -> dict:
    """步骤 → {"step", "tool_hint"}；hint 只表达"需要什么档次的工具"。

    判定顺序保持历史行为：检索类关键词优先（"联网搜索并写文件"仍算只读档），
    再看写/命令类关键词。
    """
    s = step
    if any(k in s for k in _READ_KEYWORDS):
        tool_hint = _PLAN_HINT_TOOL
    elif any(k in s for k in _WRITE_KEYWORDS):
        tool_hint = _PLAN_HINT_WRITE
    else:
        tool_hint = ""
    return {"step": s, "tool_hint": tool_hint}


_TASK_MODE_KEYWORDS = (
    "完成", "实现", "开发", "搭建", "重构", "改造", "整个项目", "端到端",
    "从零", "全部做完", "把这个项目", "做完整个",
)


def _is_project_task(
    settings,
    question: str,
    plan_steps: list[str],
    todos: list | None = None,
    plan_map: list[dict] | None = None,
) -> bool:
    """识别项目级任务：关键词 / 计划含写文件或命令步骤 / 有活跃任务清单 /
    长问题且计划需要工具。

    第 2 条替代了原来的"计划步数≥4"：步数只反映问题长度，与任务量无关——
    3 步的"设计 HTML 页面并测试"曾因此掉到非任务预算（实测 run 403/404
    两轮都被强制收尾）。判据是**步骤是否有副作用**（写文件/跑命令），
    与具体用哪个工具无关。

    "有活跃任务清单"这条兜住"继续"类追问：实测 _is_project_task("继续", [])
    恒为 False，导致追问轮预算掉回 12 次。

    运行中还有两条升格路径（见 tools.dispatch/敏感工具）：声明了 execute
    子任务、或真的动手写文件/跑命令，也会切到项目级预算。
    """
    if not effective(settings, "task_mode_detect"):
        return False
    q = question or ""
    if any(k in q for k in _TASK_MODE_KEYWORDS):
        return True
    steps = plan_steps or []
    hints: list = [
        (m.get("tool_hint") if isinstance(m, dict) else None)
        for m in (plan_map or [])
    ]
    if not any(hints):
        # 无 plan_map（resume/恢复/规划失败）时回退关键词推断
        hints = [_plan_hint(s).get("tool_hint") for s in steps]
    if any(h == _PLAN_HINT_WRITE for h in hints):
        return True
    if any(not (t or {}).get("done") for t in (todos or [])):
        return True
    return len(q) >= 80 and any(hints)


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
    """生成"执行摘要"：基于计划+工具轨迹，用低温模型写一段简短的执行说明。

    注意这是**事后叙述**，不是模型真实的推理过程（真实思考在
    reasoning_content 里）。所以提示词里明确要求"只陈述实际做了什么"，
    不要求它描述"如何分析、为什么这样回答"——那会诱导模型编造推理。
    """
    trace_lines = "\n".join(
        f"- 调用 {t.get('name')}：{str(t.get('summary') or '')[:80]}"
        for t in (state.get("tool_trace") or [])[:4]
    )
    prompt = (
        "根据下面的执行过程，用第一人称写一段不超过 120 字的【执行摘要】。"
        "只陈述实际做了什么（检索/搜索了什么、得到什么结论），"
        "不要描述'我如何分析''我为什么这样回答'这类推理过程——那是编造。"
        "只输出摘要本身，不要解释。\n\n"
        f"问题：{state['question'][:300]}\n"
        f"计划：{'；'.join(state.get('plan_steps') or [])}\n"
        f"工具过程：\n{trace_lines or '（无工具调用）'}"
    )
    try:
        resp = service.context._invoke([HumanMessage(content=prompt)])
        return message_text(resp.content).strip()[:200]
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


