"""计划模式（enter/exit_plan_mode）与 ask_user_question 工具。"""

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

logger = logging.getLogger(__name__)


PLAN_ENTER_TOOL_NAME = "enter_plan_mode"


PLAN_EXIT_TOOL_NAME = "exit_plan_mode"


PLAN_MODE_SYSTEM_HINT = (
    "# 计划模式（只读探索与方案提交）\n"
    "你已进入计划模式：只能读取/检索/搜索来了解现状，"
    "写文件、编辑、删除、执行命令都会被系统拦截。\n"
    "请先用只读手段把现状摸清（必要时用 dispatch_subtasks 并行探索，有关键分叉点可调 ask_user_question 询问），"
    "然后输出一份可执行的完整计划（步骤、每步要动什么、预期影响、需要用户确认的点），"
    "并**必须调用 exit_plan_mode 提交结构化步骤**（不要只把计划写成正文）。用户确认后，系统会按同一份计划发起执行。"
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


ASK_QUESTION_TOOL_NAME = "ask_user_question"


_ASK_DESCRIPTION = (
    "向用户提 1~4 个选择题，等回答后继续本轮（不是结束对话）。\n"
    "适合：执行中途需要用户做分支决策（选方案/范围/优先级），不同回答走不同路径。\n"
    "不适合：普通澄清（直接正文问）或能从代码/知识库查到的（先自己查）。\n"
    "questions 每项：{question, header?, multi_select?, "
    "options:[{label, description?}]}，每题 2~4 个互斥选项；"
    "用户还可自由输入或跳过。一次把相关问题问完。"
)


class _AskArgs(BaseModel):
    questions: list[dict] = Field(
        description=(
            "1~4 个问题。每项字段：question（必填）、header（短标题）、"
            "multi_select（布尔）、options（2~4 个 {label, description}，label 必填）"
        )
    )


def make_ask_user_question_tool() -> BaseTool:
    """向用户提问（无副作用：真正挂起在 tools 节点）。"""

    def _invoke(questions: list[dict]) -> dict:
        return {"summary": "问题已提交，等待用户回答"}

    return StructuredTool.from_function(
        func=_invoke,
        name=ASK_QUESTION_TOOL_NAME,
        description=_ASK_DESCRIPTION,
        args_schema=_AskArgs,
    )
