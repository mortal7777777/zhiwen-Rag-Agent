"""子代理域：dispatch/subagent/merge 节点与辅助（Send 并行、受限工具、结论合并，自 langgraph_agent.py 拆分）。"""

from __future__ import annotations

import json
import logging

from langgraph.types import Send
from sqlalchemy.orm import Session

from ...runtime_config import effective

logger = logging.getLogger(__name__)
from ..state import AgentState, EventBus
from .common import _plan_hint

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..langgraph_agent import LangGraphAgentService  # 仅类型注解
from ...tracing import get_aux_usage_collector

# ==== 函数体（原文）====
def _build_subagent_tasks(state: AgentState) -> list[dict]:
    """把计划中带工具提示的步骤拆成子任务（最多 4 个）。"""
    tasks: list[dict] = []
    seen: set[str] = set()
    for item in state.get("plan_map") or []:
        hint = item.get("tool_hint") or ""
        step = (item.get("step") or "").strip()
        if not hint or not step:
            continue
        key = f"{hint}|{step[:40]}"
        if key in seen:
            continue
        seen.add(key)
        tasks.append({"step": step, "tool_hint": hint})
    return tasks[:4]


def _renumber_subagent_sources(results: list[dict], counter: list[int]) -> list[dict]:
    """把各子代理的局部引用编号重排为全局编号，避免分支间 [n] 冲突。"""
    merged: list[dict] = []
    for r in results or []:
        for s in r.get("sources") or []:
            counter[0] += 1
            item = dict(s)
            item["index"] = counter[0]
            merged.append(item)
    return merged


def _remaining_needs_tools(remaining: list[dict]) -> bool:
    """剩余计划步骤里是否还有需要工具的步骤（用于计划硬约束判断）。"""
    return any(
        (
            item.get("tool_hint")
            if isinstance(item, dict) and "tool_hint" in item
            else _plan_hint(
                str(item.get("text") or item.get("step") or item)
            ).get("tool_hint")
        )
        for item in remaining or []
    )


def _subagent_tools(
    service: "LangGraphAgentService",
    hint: str,
    counter: list[int],
    project_dir: str | None = None,
    sandbox: str | None = None,
):
    """按工具提示给子代理构建受限工具集（只读/检索类，不含敏感操作）。"""
    from ...tools_extra import (
        make_bash_tool,
        make_delete_file_tool,
        make_edit_file_tool,
        make_grep_search_tool,
        make_list_dir_tool,
        make_read_file_tool,
        make_write_file_tool,
    )
    from ..tools import make_knowledge_base_tool, make_web_search_tool

    settings = service.settings
    if hint == "knowledge_base_search":
        return [
            make_knowledge_base_tool(
                service.rag,
                counter,
                settings.crag_fallback_enabled,
                settings.crag_min_score,
                effective(settings, "web_search_provider"),
                settings.tavily_api_key,
                effective(settings, "web_search_max_results"),
                allow_web_fallback=False,
                searxng_base_url=effective(settings, "searxng_base_url") or "",
                searxng_engines=effective(settings, "searxng_engines") or "",
            )
        ]
    if hint == "web_search":
        return [
            make_web_search_tool(
                effective(settings, "web_search_provider"),
                settings.tavily_api_key,
                effective(settings, "web_search_max_results"),
                counter,
                searxng_base_url=effective(settings, "searxng_base_url") or "",
                searxng_engines=effective(settings, "searxng_engines") or "",
            )
        ]
    if hint in ("file_tool/bash",):
        return [
            make_list_dir_tool(settings, project_dir),
            make_read_file_tool(settings, project_dir),
            make_grep_search_tool(settings, project_dir),
            make_write_file_tool(settings, project_dir),
            make_edit_file_tool(settings, project_dir),
            make_delete_file_tool(settings, project_dir),
            make_bash_tool(settings, project_dir, sandbox),
        ]
    return []


def _dispatch_node(state: AgentState) -> dict:
    """fan-out 占位节点：真正的 Send 由条件边函数 _dispatch_tasks 发出。"""
    return {}


def _dispatch_tasks(state: AgentState):
    """把每个子任务 Send 到独立上下文的 subagent 节点（并行 superstep）。"""
    return [
        Send(
            "subagent",
            {
                "sub_task": task,
                "service": state["service"],
                "question": state["question"],
                "bus": state["bus"],
                "runtime": state["runtime"],
                "db": state["db"],
                "stop_event": state.get("stop_event"),
                "project_dir": state.get("project_dir"),
            },
        )
        for task in _build_subagent_tasks(state)
    ]


def _run_sensitive_subagent_tool(
    service: "LangGraphAgentService",
    permission_manager,
    name: str,
    args: dict,
    invoke_fn,
    tool,
    bus,
    runtime: dict,
    stop_event,
) -> dict:
    """子代理执行敏感工具：白名单自动放行，否则走与主 Agent 一致的 HITL。"""
    from ...permissions import describe_tool_call, display_args
    from ...tools_extra import command_allowed

    settings = service.settings
    command = str(args.get("command") or "")
    allowed_cmd = (
        name in ("bash", "command_tool")
        and (
            command_allowed(settings, command)[0]
            or permission_manager.is_session_allowed(runtime.get("conv_id"), command)
        )
    )
    mode = effective(settings, "tool_permission_mode") or "ask"
    if mode == "allow" or allowed_cmd:
        return invoke_fn(tool, args)

    summary = describe_tool_call(name, args)
    req = permission_manager.submit(
        tool=name,
        arguments=display_args(name, args),
        summary=summary,
        conversation_id=runtime.get("conv_id"),
    )
    if bus is not None:
        bus.emit(
            "permission_request",
            {
                "id": req.id,
                "name": name,
                "arguments": req.arguments,
                "summary": summary,
            },
        )
        bus.emit(
            "status",
            {"phase": "permission", "text": f"子代理等待确认：{summary}"},
        )
    approved = permission_manager.wait(
        req,
        timeout=int(effective(settings, "permission_timeout", 0) or 0),
        stop_event=stop_event,
    )
    if approved:
        if bus is not None:
            bus.emit(
                "permission_resolved",
                {"id": req.id, "approved": True, "reason": ""},
            )
        if req.remember_session and command:
            permission_manager.mark_session_allowed(runtime.get("conv_id"), command)
        result = invoke_fn(tool, args)
        if req.remember_forever and command:
            result["summary"] = (
                str(result.get("summary") or "") + "（已记住，下次不再询问）"
            )
        return result
    reason = req.reason or (
        "用户拒绝了该操作"
        if req.status == "denied"
        else "等待确认超时，已自动取消"
    )
    if bus is not None:
        bus.emit(
            "permission_resolved",
            {"id": req.id, "approved": False, "reason": reason},
        )
    return {
        "summary": f"用户拒绝了该操作：{reason}",
        "permission": "denied",
        "error": f"操作未执行（用户拒绝）：{reason}。请说明影响并询问替代方案。",
    }


def _subagent_node(state: AgentState) -> dict:
    """一个子代理分支：独立 messages + 受限工具，敏感操作走 HITL，返回结论摘要。"""
    from ...permissions import (
        describe_tool_call,
        display_args,
        get_permission_manager,
        is_sensitive_tool,
    )
    from ...tools_extra import command_allowed
    from ...hooks import run_hooks
    from ..tools import extract_sources

    service: LangGraphAgentService = state["service"]
    settings = service.settings
    bus: EventBus | None = state.get("bus")
    runtime: dict = state.get("runtime") or {}
    question = state.get("question") or ""
    task = state.get("sub_task") or {}
    step = task.get("step") or ""
    hint = task.get("tool_hint") or ""
    counter: list[int] = [0]
    tools = _subagent_tools(
        service, hint, counter, state.get("project_dir"),
        sandbox=runtime.get("command_sandbox"),
    )

    messages = [
        SystemMessage(content=SUBAGENT_SYSTEM_PROMPT),
        HumanMessage(content=f"原始问题：{question}\n\n子任务：{step}"),
    ]
    summary = ""
    sources_local: list[dict] = []
    trace_local: list[dict] = []
    stop_event = state.get("stop_event")
    permission_manager = get_permission_manager()
    max_rounds = max(1, int(effective(settings, "agent_subagent_max_rounds") or 2))

    def _invoke(tool, args: dict) -> dict:
        try:
            out = tool.invoke(args)
            return out if isinstance(out, dict) else {"result": out}
        except Exception as exc:
            return {"error": str(exc)}

    service.rag.acquire_llm()
    try:
        for _round in range(max_rounds):
            if stop_event is not None and stop_event.is_set():
                break
            chat = service.chat.bind_tools(tools) if tools else service.chat
            resp = chat.invoke(
                messages,
                config={"callbacks": [get_aux_usage_collector()]},
            )
            messages.append(resp)
            tool_calls = list(getattr(resp, "tool_calls", None) or [])
            if not tool_calls:
                summary = resp.content or ""
                break
            by_name = {t.name: t for t in tools}
            for tc in tool_calls:
                name = tc.get("name")
                tool = by_name.get(name)
                args = tc.get("args") or {}
                if tool is None:
                    result: dict = {"error": f"子代理不可用工具：{name}"}
                elif is_sensitive_tool(name, args):
                    result = _run_sensitive_subagent_tool(
                        service,
                        permission_manager,
                        name,
                        args,
                        _invoke,
                        tool,
                        bus,
                        runtime,
                        stop_event,
                    )
                else:
                    result = _invoke(tool, args)
                messages.append(
                    ToolMessage(
                        content=json.dumps(result, ensure_ascii=False),
                        name=name or "",
                        tool_call_id=tc.get("id") or "",
                    )
                )
                trace_local.append(
                    {
                        "name": name,
                        "arguments": args,
                        "summary": result.get("summary", ""),
                        "duration_ms": None,
                    }
                )
                if name in ("knowledge_base_search", "web_search"):
                    sources_local.extend(extract_sources(result))
        if not summary and (stop_event is None or not stop_event.is_set()):
            # 兜底：两轮都还在调工具时，强制补一次结论摘要，保证主 Agent 拿到结果
            try:
                messages.append(
                    SystemMessage(
                        content="请基于上面的工具结果，用 2~4 句话输出本子任务的结论摘要。"
                    )
                )
                final_resp = service.chat.invoke(
                    messages,
                    config={"callbacks": [get_aux_usage_collector()]},
                )
                summary = final_resp.content or ""
            except Exception as exc:
                summary = f"（子任务总结失败：{exc}）"
    finally:
        service.rag.release_llm()

    # 清洗子代理输出中的 XML 工具调用标记，防止泄漏到主 agent 的 tool_result 摘要
    if summary:
        import re as _re
        summary = _re.sub(r"</?tool_calls[^>]*>|</?invoke[^>]*>|</?parameter[^>]*>|</?tool_use[^>]*>|</?function[^>]*>", "", summary)
        summary = _re.sub(r"<\|", "<", summary)  # 清理 <user|tool_calls> 等变体
        summary = summary.strip()[:800]

    return {
        "subagent_results": [
            {
                "name": step[:40],
                "task": step,
                "summary": (summary or "（子任务未产出结论）")[:800],
                "sources": sources_local,
                "tool_trace": trace_local,
            }
        ]
    }


def _merge_node(state: AgentState) -> dict:
    """fan-in：汇总各子代理结果，合并来源与轨迹，同步任务清单。"""
    from ...todos import plan_progress, sync_todos_from_plan

    bus: EventBus = state["bus"]
    db: Session | None = state["db"]
    runtime: dict = state["runtime"]
    messages = state["messages"]
    results = state.get("subagent_results") or []
    counter = state["counter"]

    blocks: list[str] = []
    merged_sources: list[dict] = _renumber_subagent_sources(results, counter)
    for r in results:
        # 清洗子代理摘要中的 XML 工具调用标记（兜底输出泄漏）
        summary_text = str(r.get("summary") or "")
        if summary_text:
            import re as _re_merge
            summary_text = _re_merge.sub(
                r"</?tool_calls[^>]*>|</?invoke[^>]*>|</?parameter[^>]*>|</?tool_use[^>]*>|</?function[^>]*>|</?arguments[^>]*>",
                "", summary_text,
            )
            summary_text = _re_merge.sub(r"<[|]", "<", summary_text)
            summary_text = summary_text.strip()
        blocks.append(
            f"### {r.get('name') or r.get('task', '')}\n{summary_text}"
        )
        for t in r.get("tool_trace") or []:
            entry = dict(t)
            entry["step"] = len(state["tool_trace"]) + 1
            state["tool_trace"].append(entry)
        bus.emit(
            "tool_start",
            {
                "id": f"subagent_{counter[0]}",
                "name": "subagent",
                "arguments": {"task": str(r.get("task") or "")[:80]},
            },
        )
        bus.emit(
            "tool_result",
            {
                "id": f"subagent_{counter[0]}",
                "name": "subagent",
                "summary": summary_text[:120],
                "duration_ms": None,
                "sources": r.get("sources") or [],
            },
        )

    state["sources"].extend(merged_sources)
    if blocks:
        messages.append(
            SystemMessage(
                content=(
                    "以下是对应各子任务的并行结果摘要（已完成，引用来源已合并进索引）：\n\n"
                    + "\n\n".join(blocks)
                )
            )
        )

    plan_steps = state.get("plan_steps") or []
    plan_map = state.get("plan_map") or []
    todos = state.get("todos") or []
    if plan_steps and db is not None and runtime.get("conv_id"):
        target = sum(1 for i in plan_map if i.get("tool_hint"))
        hints = [_plan_hint(s).get("tool_hint") for s in plan_steps]
        todos = (
            sync_todos_from_plan(
                db, runtime["conv_id"], plan_steps, target, hints=hints
            )
            or todos
        )
        done, total, remaining = plan_progress(todos)
        state["plan_done_count"] = done
        state["todos"] = todos
        bus.emit("todos", {"todos": todos})
        if total:
            bus.emit(
                "plan_progress",
                {
                    "done": done,
                    "total": total,
                    "current": remaining[0].get("text") if remaining else None,
                    "text": f"计划进度：已完成 {done}/{total} 步（子代理并行）。",
                },
            )

    state["dispatch_done"] = True
    return {
        "messages": messages,
        "tool_trace": state["tool_trace"],
        "sources": state["sources"],
        "plan_done_count": state.get("plan_done_count", 0),
        "todos": state.get("todos") or [],
        "dispatch_done": True,
    }


# ============================================================
# 节点：agent（ReAct 思考 + 流式生成）
# ============================================================



SUBAGENT_SYSTEM_PROMPT = (
    "你是主 Agent 派出的并行子任务代理。只完成分配给你的这一个子步骤："
    "必要时调用可用工具获取信息，然后用 2~4 句话输出该子步骤的结论摘要。"
    "不要输出最终答案、不要调用与子任务无关的工具；写文件/执行命令等敏感操作"
    "会由系统请求用户确认，被拒绝时如实说明影响并询问替代方案。"
)


