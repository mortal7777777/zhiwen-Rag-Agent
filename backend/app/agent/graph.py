"""LangGraph 图定义与条件路由（图即文档；自 langgraph_agent.py 拆分）。"""

from __future__ import annotations

import logging
import time

from langgraph.graph import END, START, StateGraph

from ..runtime_config import effective

logger = logging.getLogger(__name__)
from .state import AgentState
from .nodes.agent import _agent_node
from .nodes.finalize import _finalize_node
from .nodes.prepare import _prepare_node
from .nodes.subagent import (
    _build_subagent_tasks,
    _dispatch_node,
    _dispatch_tasks,
    _merge_node,
    _subagent_node,
)
from .nodes.tools import _tools_node

# ==== 函数体（原文）====
def _route_after_prepare(state: AgentState) -> str:
    if state.get("stop_event") is not None and state["stop_event"].is_set():
        return "agent"
    subagents_on = True
    if state.get("service") is not None:
        val = effective(state["service"].settings, "agent_subagents_enabled")
        subagents_on = val is not False
    if (
        subagents_on
        and not state.get("dispatch_done")
        and _build_subagent_tasks(state)
    ):
        return "dispatch"
    return "agent"


def _route_after_agent(state: AgentState) -> str:
    if state.get("stop_event") is not None and state["stop_event"].is_set():
        return "finalize"
    if state.get("pending_tool_calls"):
        return "tools"
    if state.get("force_continue"):
        # 计划硬约束：未完成的工具型步骤存在时，不允许提前收尾
        return "agent"
    return "finalize"


def _route_after_tools(state: AgentState) -> str:
    if state.get("stop_event") is not None and state["stop_event"].is_set():
        return "finalize"
    return "agent"


def _timed_node(name: str, fn):
    """节点耗时打点包装器：把每个图节点的墙钟耗时累加进 runtime.timings。

    subagent 每个分支、agent/tools 每一轮都进同一节点函数，
    用 += 累加得到该阶段的总耗时；finalize 落库到 agent_runs.token_usage.timings。
    """

    def wrapped(state: AgentState) -> dict:
        runtime = state.get("runtime")
        if runtime is None:
            runtime = {}
            state["runtime"] = runtime
        timings = runtime.setdefault("timings", {})
        t0 = time.perf_counter()
        try:
            return fn(state)
        finally:
            timings[f"{name}_ms"] = (
                timings.get(f"{name}_ms", 0)
                + round((time.perf_counter() - t0) * 1000)
            )

    return wrapped


def build_agent_graph(checkpointer=None):
    """构建 LangGraph：prepare -> [dispatch -> subagents -> merge] -> agent -> tools -> finalize。"""
    graph = StateGraph(AgentState)
    graph.add_node("prepare", _timed_node("prepare", _prepare_node))
    graph.add_node("dispatch", _timed_node("dispatch", _dispatch_node))
    graph.add_node("subagent", _timed_node("subagent", _subagent_node))
    graph.add_node("merge", _timed_node("merge", _merge_node))
    graph.add_node("agent", _timed_node("agent", _agent_node))
    graph.add_node("tools", _timed_node("tools", _tools_node))
    graph.add_node("finalize", _timed_node("finalize", _finalize_node))

    graph.add_edge(START, "prepare")
    graph.add_conditional_edges(
        "prepare",
        _route_after_prepare,
        {"dispatch": "dispatch", "agent": "agent"},
    )
    graph.add_conditional_edges("dispatch", _dispatch_tasks, ["subagent"])
    graph.add_edge("subagent", "merge")
    graph.add_edge("merge", "agent")
    graph.add_conditional_edges(
        "agent",
        _route_after_agent,
        {"tools": "tools", "agent": "agent", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "tools",
        _route_after_tools,
        {"agent": "agent", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


# ============================================================
# 服务：LangGraphAgentService
# ============================================================


