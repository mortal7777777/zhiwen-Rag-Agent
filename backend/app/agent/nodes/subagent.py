"""子代理域：dispatch/subagent/merge 节点与辅助（Send 并行、受限工具、结论合并，自 langgraph_agent.py 拆分）。"""

from __future__ import annotations

import json
import logging
import os
import time

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import Send
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...llm_text import message_text
from ...runtime_config import effective

logger = logging.getLogger(__name__)
from ..state import AgentState, EventBus
from ..utils import _run_verify
from .common import _plan_hint

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..langgraph_agent import LangGraphAgentService  # 仅类型注解
from ...tracing import get_aux_usage_collector

# ==== 函数体（原文）====
# ---- 声明式派发：由主代理（而非 planner + 规则层）决定派什么 ----
# 2026-09-24 事故：planner 只看单条问题、不知道对话与磁盘现状，规则层又
# "hint 非空即派发"，于是"编写 HTML 文件"被派给隔离上下文的子代理，它发现
# 旧文件后当作成果汇报，主 agent 整轮没产出。改为：主代理用本工具**声明**
# 子任务，条件边扇出（保留 Send 并行与 merge），执行结果以工具返回值回到
# 主代理（可核验、可重派），而不是"（已完成）"式的系统断言。
DISPATCH_TOOL_NAME = "dispatch_subtasks"
DISPATCH_ARGS_EXAMPLE = 'dispatch_subtasks({"task": "...", "mode": "research|execute"})'

MODE_RESEARCH = "research"
MODE_EXECUTE = "execute"
_EXECUTE_ALIASES = {"execute", "write", "bash", "command", "run"}
_MAX_SUBTASKS_PER_ROUND = 4

_DISPATCH_DESCRIPTION = (
    "把一个**自包含的子任务**交给并行子代理执行（每轮可调多次，最多 4 个）。\n"
    "\n"
    "值得派发：① 多路互不依赖的检索/阅读（会真并行执行）；"
    "② 需要读大文件、而你只需要结论摘要（原始内容不会占用你的上下文）；"
    "③ 目标明确、可独立验收的执行任务（写文件/改文件/跑命令）。\n"
    "不值得派发：一两步就能做完的琐碎操作；后一步依赖前一步结果的串行工作；"
    "需要你本人基于完整对话上下文判断的事。\n"
    "\n"
    "task 必须自包含——子代理看不到你们的对话历史，只能看到你写的 task 描述、"
    "工作目录与顶层文件清单。请写清：目标、产出物路径、验收标准、禁止事项"
    "（例如\"不要复用目录里已有的同类文件，必须新建\"）。\n"
    "mode=research 只给只读工具（检索/列目录/读文件/搜索）；"
    "mode=execute 才给写文件与命令工具，且**每轮只放开一个 execute 子任务**"
    "（并行写同一目录有冲突风险），其余会被降级为 research。\n"
    "\n"
    "回报是子代理的**自述结论 + 产物证据**（新建/覆盖、字节数、行数、命令与退出码、"
    "写后校验结果），未经你核实。与你的判断冲突时，可以重新派发或自己动手做。"
)


class _DispatchArgs(BaseModel):
    """dispatch_subtasks 的参数（结构化约束比自由文本可靠）。"""

    task: str = Field(
        description=(
            "自包含的子任务描述：目标 + 产出物路径 + 验收标准 + 禁止事项。"
            "子代理看不到对话历史，写不进去的约束它就不可能遵守。"
        )
    )
    mode: str = Field(
        default=MODE_RESEARCH,
        description=(
            "research=只读（检索/列目录/读文件/搜索，可并行）；"
            "execute=需要写文件或执行命令（每轮只放开一个）"
        ),
    )


def make_dispatch_tool(settings) -> BaseTool:
    """声明式派发工具的**定义**（不执行副作用，由编排层扇出）。

    仅供 bind_tools 使用：agent 节点会把它从 pending_tool_calls 里摘出来放进
    pending_subtasks，由图条件边走 Send 并行——若与普通工具混在同一轮，
    只受理声明并提示模型下一轮重发其余调用。
    """

    def _invoke(task: str, mode: str = MODE_RESEARCH) -> dict:
        return {"summary": "声明已受理，由编排层派发"}

    return StructuredTool.from_function(
        func=_invoke,
        name=DISPATCH_TOOL_NAME,
        description=_DISPATCH_DESCRIPTION,
        args_schema=_DispatchArgs,
    )


def _split_declaration(tool_calls: list) -> tuple[list, list]:
    """把声明调用与普通工具调用分开（同一轮只做一件事）。"""
    calls = list(tool_calls or [])
    declared = [
        tc
        for tc in calls
        if str((tc or {}).get("name") or "") == DISPATCH_TOOL_NAME
    ]
    if not declared:
        return [], calls
    others = [tc for tc in calls if tc not in declared]
    return declared, others


def _parse_declared_subtasks(declared_calls: list) -> tuple[list[dict], int]:
    """解析声明 → 子任务列表；execute 只保留第一条，其余降级为 research。

    返回 (subtasks, downgraded_shown_count)。每条带 call_id，供 merge 按
    tool_call 一一对应地回填 ToolMessage（协议要求一个 call 一条结果）。
    """
    subtasks: list[dict] = []
    for tc in declared_calls:
        args = (tc or {}).get("args") or {}
        call_id = str((tc or {}).get("id") or "")
        nested = args.get("subtasks") if isinstance(args.get("subtasks"), list) else None
        raw = nested if nested is not None else [args]
        for item in raw:
            if len(subtasks) >= _MAX_SUBTASKS_PER_ROUND:
                break
            if isinstance(item, str):
                task, mode = item.strip(), MODE_RESEARCH
            elif isinstance(item, dict):
                task = str(item.get("task") or item.get("step") or "").strip()
                mode = str(item.get("mode") or MODE_RESEARCH).strip().lower()
            else:
                continue
            if not task:
                continue
            mode = MODE_EXECUTE if mode in _EXECUTE_ALIASES else MODE_RESEARCH
            subtasks.append(
                {"task": task[:400], "mode": mode, "call_id": call_id}
            )
    seen_execute = False
    downgraded = 0
    for item in subtasks:
        if item["mode"] != MODE_EXECUTE:
            continue
        if seen_execute:
            item["mode"] = MODE_RESEARCH
            downgraded += 1
        else:
            seen_execute = True
    return subtasks, downgraded


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
    mode: str,
    counter: list[int],
    project_dir: str | None = None,
    sandbox: str | None = None,
):
    """按执行模式给子代理构建工具集。

    - research：只读（检索/联网/列目录/读文件/搜索）——可安全并行；
    - execute：追加写文件/编辑/删除/命令（敏感操作走 HITL 确认）。
    """
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
    tools: list = [
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
        ),
        make_web_search_tool(
            effective(settings, "web_search_provider"),
            settings.tavily_api_key,
            effective(settings, "web_search_max_results"),
            counter,
            searxng_base_url=effective(settings, "searxng_base_url") or "",
            searxng_engines=effective(settings, "searxng_engines") or "",
        ),
        make_list_dir_tool(settings, project_dir),
        make_read_file_tool(settings, project_dir),
        make_grep_search_tool(settings, project_dir),
    ]
    if mode == MODE_EXECUTE:
        tools.extend(
            [
                make_write_file_tool(settings, project_dir),
                make_edit_file_tool(settings, project_dir),
                make_delete_file_tool(settings, project_dir),
                make_bash_tool(settings, project_dir, sandbox),
            ]
        )
    return tools


def _build_context_pack(
    service: "LangGraphAgentService",
    state: AgentState,
    mode: str,
) -> str:
    """子代理的上下文包：工作目录 + 顶层文件清单（含 mtime）+ 本模式可用工具。

    子代理看不到主对话，只能靠这里的既有事实作判断。文件清单带修改时间是
    刻意的——2026-09-24 事故里子代理把磁盘上的历史遗留产物当成了本子任务
    成果，时间戳让"这不是本轮产出的东西"变得可判。
    """
    from ...tools_extra import _resolve_workspace

    lines: list[str] = []
    try:
        workspace = _resolve_workspace(service.settings, state.get("project_dir"))
        lines.append(f"工作目录：{workspace}")
        entries = []
        for name in sorted(os.listdir(workspace))[:60]:
            if name.startswith(".") or name in ("node_modules", "__pycache__"):
                continue
            full = os.path.join(workspace, name)
            try:
                stat = os.stat(full)
                kind = "/" if os.path.isdir(full) else ""
                stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
                entries.append(f"  - {name}{kind}  ({stat.st_size} B, {stamp})")
            except OSError:
                entries.append(f"  - {name}")
        if entries:
            lines.append(
                "工作目录顶层内容（含修改时间，用于识别历史遗留产物）：\n"
                + "\n".join(entries[:40])
            )
    except Exception as exc:  # 目录不可达不该拖垮子任务
        logger.warning("子代理上下文包构建失败：%s", exc)
    tool_names = [
        "knowledge_base_search", "web_search", "list_dir", "read_file",
        "grep_search",
    ]
    if mode == MODE_EXECUTE:
        tool_names += ["write_file", "edit_file", "delete_file", "bash"]
    lines.append("本子任务可用工具：" + "、".join(tool_names))
    return "\n\n".join(lines)


_CAUTION_MARKERS = (
    "未找到", "未产出", "不确定", "失败", "错误", "历史遗留", "拒绝", "缺少", "冲突",
)


def _todo_label(task, limit: int = 60) -> str:
    """子任务 → 任务清单里的一行短标签。

    声明里的 task 可以写得很长（自包含要求），但清单要能一眼看完：
    取首行/首句并截断，避免 UI 出现整屏文字（实测一次声明的 todo 有 200 字）。
    """
    text = str(task or "").strip()
    if not text:
        return ""
    first = text.splitlines()[0].strip()
    for prefix in ("目标：", "子任务：", "任务："):
        if first.startswith(prefix):
            first = first[len(prefix) :].strip()
            break
    for sep in ("。", "；", "!"):
        idx = first.find(sep)
        if 0 < idx <= limit:
            first = first[:idx]
            break
    return first.strip()[:limit]


def _truncate_summary(text: str, limit: int = 1200) -> str:
    """摘要截断：优先保住风险提示行，别把"未找到/失败/历史遗留"截掉。

    旧实现直接 [:800]，实测可能正好切掉告警句，主 agent 只看到像结论的
    前半段（2026-09-24 复用事故的放大机制之一）。
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    keep: list[str] = []
    rest: list[str] = []
    for line in text.splitlines():
        (keep if any(m in line for m in _CAUTION_MARKERS) else rest).append(line)
    out = "\n".join(keep)[: limit // 2]
    for line in rest:
        if len(out) + len(line) + 1 > limit:
            break
        out = f"{out}\n{line}" if out else line
    return out.strip()[:limit]


def _dispatch_node(state: AgentState) -> dict:
    """受理声明：把子任务落进任务清单（硬约束的种子），并升格任务态预算。

    真正的 Send 扇出由条件边函数 _dispatch_tasks 发出；这里只做落库与状态同步。
    """
    from ...todos import append_plan_todos, plan_progress

    subtasks = state.get("pending_subtasks") or []
    bus: EventBus | None = state.get("bus")
    db: Session | None = state.get("db")
    runtime: dict = state.get("runtime") or {}
    steps: list[str] = []
    for task in subtasks:
        label = _todo_label(task.get("task"))
        if label and label not in steps:
            steps.append(label)

    # 声明就是本轮的工作清单：追加进 todos，硬约束据此生效
    # （无 todos 时 agent.py 的计划硬约束恒不触发，声明轮之后就成了"无约束"）
    todos = state.get("todos") or []
    if db is not None and runtime.get("conv_id") and steps:
        try:
            todos = append_plan_todos(db, runtime["conv_id"], steps) or todos
        except Exception as exc:
            logger.warning("子任务落清单失败：%s", exc)
    done, total, remaining = plan_progress(todos)
    if bus is not None:
        for task in subtasks:
            bus.emit(
                "tool_start",
                {
                    "id": f"subtask_{task.get('call_id') or ''}_{abs(hash(task.get('task') or '')) % 10**6}",
                    "name": "subagent",
                    "arguments": {
                        "task": str(task.get("task") or "")[:120],
                        "mode": task.get("mode"),
                    },
                },
            )
        if total:
            bus.emit(
                "plan_progress",
                {
                    "done": done,
                    "total": total,
                    "current": remaining[0].get("text") if remaining else None,
                    "text": f"已派发 {len(subtasks)} 个子任务（并行执行中）。",
                },
            )

    return {
        "todos": todos,
        "plan_done_count": done,
        # 声明含 execute → 按项目级任务给预算（写文件/命令类工作）
        "task_mode": bool(state.get("task_mode"))
        or any(t.get("mode") == MODE_EXECUTE for t in subtasks),
    }


def _dispatch_tasks(state: AgentState):
    """把每个子任务 Send 到独立上下文的 subagent 节点（并行 superstep）。"""
    subtasks = state.get("pending_subtasks") or []
    if not subtasks:
        # 声明为空（异常路径）：直接进 merge 回一条提示，避免"空扇出"让图
        # 静默结束（那样连 finalize 都不会跑，回答与落库全丢）。
        # Send 的 payload 就是该节点看到的 state，所以这里显式带齐 merge 要用的键。
        logger.warning("派发声明为空，跳过扇出直接合并")
        return [
            Send(
                "merge",
                {
                    "service": state["service"],
                    "bus": state["bus"],
                    "runtime": state["runtime"],
                    "db": state["db"],
                    "question": state.get("question"),
                    "stop_event": state.get("stop_event"),
                    "messages": state.get("messages") or [],
                    "sources": state.get("sources") or [],
                    "counter": state.get("counter") or [0],
                    "tool_trace": state.get("tool_trace") or [],
                    "subagent_results": state.get("subagent_results") or [],
                    "subagent_consumed": int(
                        state.get("subagent_consumed") or 0
                    ),
                    "dispatch_rounds": int(state.get("dispatch_rounds") or 0),
                    "todos": state.get("todos") or [],
                    "plan_done_count": int(state.get("plan_done_count") or 0),
                },
            )
        ]
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
        for task in subtasks
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


def _collect_evidence(
    name: str,
    args: dict,
    result: dict,
    artifacts: list[dict],
    commands: list[dict],
    files_read: list[str],
) -> None:
    """收集**可核验的产物证据**——子代理自述之外，唯一能让主 agent 判断
    "这一步到底产出了什么"的硬事实（2026-09-24：只有散文摘要时，主 agent
    无法区分"我产出了 X"和"磁盘上本来就有 X"）。"""
    if not isinstance(result, dict) or result.get("error"):
        return
    if name in ("write_file", "edit_file"):
        rel = str(result.get("relative_path") or args.get("path") or "")
        if rel:
            artifacts.append(
                {
                    "path": rel,
                    "action": "created" if result.get("created") else "modified",
                    "bytes": result.get("bytes"),
                    "lines": result.get("lines"),
                }
            )
    elif name == "delete_file":
        artifacts.append(
            {"path": str(args.get("path") or ""), "action": "deleted"}
        )
    elif name in ("bash", "command_tool"):
        commands.append(
            {
                "command": str(args.get("command") or "")[:200],
                "exit_code": result.get("exit_code"),
            }
        )
    elif name in ("read_file", "grep_search", "list_dir"):
        target = str(args.get("path") or args.get("pattern") or "")
        if target and target not in files_read:
            files_read.append(target[:200])


def _subagent_node(state: AgentState) -> dict:
    """一个子代理分支：独立 messages + 受限工具，敏感操作走 HITL，
    返回结论摘要 + 产物证据。"""
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
    step = str(task.get("task") or task.get("step") or "")
    mode = str(task.get("mode") or MODE_RESEARCH)
    call_id = str(task.get("call_id") or "")
    counter: list[int] = [0]
    tools = _subagent_tools(
        service, mode, counter, state.get("project_dir"),
        sandbox=runtime.get("command_sandbox"),
    )

    pack = _build_context_pack(service, state, mode)
    mode_label = (
        "execute（可写文件、可执行命令；敏感操作需用户确认）"
        if mode == MODE_EXECUTE
        else "research（只读，禁止写入）"
    )
    messages = [
        SystemMessage(content=SUBAGENT_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"原始问题：{question}\n\n"
                f"子任务：{step}\n\n"
                f"执行模式：{mode_label}\n\n{pack}"
            )
        ),
    ]
    summary = ""
    sources_local: list[dict] = []
    trace_local: list[dict] = []
    artifacts: list[dict] = []
    commands: list[dict] = []
    files_read: list[str] = []
    verify_results: list[dict] = []
    stop_event = state.get("stop_event")
    permission_manager = get_permission_manager()
    if mode == MODE_EXECUTE:
        # 执行类至少要 写→验→修 三轮，2 轮根本做不完（旧配置一刀切 2 轮）
        max_rounds = max(
            1, int(effective(settings, "agent_subagent_exec_max_rounds") or 6)
        )
    else:
        max_rounds = max(
            1, int(effective(settings, "agent_subagent_max_rounds") or 2)
        )

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
                summary = message_text(resp.content)
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
                _collect_evidence(
                    name, args, result, artifacts, commands, files_read
                )
                if name in ("knowledge_base_search", "web_search"):
                    sources_local.extend(extract_sources(result))
        if not summary and (stop_event is None or not stop_event.is_set()):
            # 兜底：轮数用尽仍在调工具时，强制补一次结论摘要，保证主 Agent 拿到结果
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
                summary = message_text(final_resp.content)
            except Exception as exc:
                summary = f"（子任务总结失败：{exc}）"
    finally:
        service.rag.release_llm()

    # 写后验证：子代理此前完全不校验产物，主循环却有 _run_verify。
    # 产物不可信时至少要让证据里出现"校验没过"。
    written = [
        str(a.get("path"))
        for a in artifacts
        if a.get("path") and a.get("action") in ("created", "modified")
    ]
    if written:
        try:
            verify_results = _run_verify(settings, written)
        except Exception as exc:  # 验证失败不该拖垮子任务
            logger.warning("子代理写后验证失败：%s", exc)

    # 清洗子代理输出中的 XML 工具调用标记，防止泄漏到主 agent 的 tool_result 摘要
    if summary:
        import re as _re
        summary = _re.sub(r"</?tool_calls[^>]*>|</?invoke[^>]*>|</?parameter[^>]*>|</?tool_use[^>]*>|</?function[^>]*>", "", summary)
        summary = _re.sub(r"<\|", "<", summary)  # 清理 <user|tool_calls> 等变体
        summary = _truncate_summary(summary)

    if not artifacts and mode == MODE_EXECUTE:
        # 执行类子任务却没产出任何文件：这是重要事实，必须显式汇报而不是让
        # 主 agent 从一段散文里自己猜（2026-09-24：子代理把旧文件当成果）。
        summary = (
            (summary or "")
            + "\n\n【产物证据】本子任务未创建或修改任何文件。"
        ).strip()

    return {
        "subagent_results": [
            {
                "name": step[:40],
                "task": step,
                "mode": mode,
                "call_id": call_id,
                "summary": summary or "（子任务未产出结论）",
                "artifacts": artifacts,
                "commands": commands,
                "files_read": files_read[:20],
                "verify": verify_results,
                "sources": sources_local,
                "tool_trace": trace_local,
            }
        ]
    }


def _merge_node(state: AgentState) -> dict:
    """fan-in：把各子代理结果按 **tool_call 一一对应**回填成 ToolMessage。

    与旧实现（system 消息断言"（已完成）"）的关键差别：结果以**工具返回值**
    进入主 agent 上下文，它天然可以怀疑、重派、自己重做——从结构上拆掉了
    "低质结果被当成既成事实"的放大机制。
    """
    from ...todos import complete_steps_by_text, load_todos, plan_progress

    bus: EventBus = state["bus"]
    db: Session | None = state["db"]
    runtime: dict = state["runtime"]
    messages = state["messages"]
    results = state.get("subagent_results") or []
    counter = state["counter"]

    # subagent_results 的归约器是 operator.add，且 state 经 checkpointer 按
    # conversation 跨轮持久化，列表只增不减。全量遍历会把此前每一轮的子代理
    # 结论重新拼进本轮摘要（实测 4 轮后单行累积到 10 个块 / 3,870 字符，
    # 整份都是缓存 miss 且此后常驻历史）。只消费本轮新增的部分。
    consumed = int(state.get("subagent_consumed") or 0)
    fresh = results[consumed:] if 0 <= consumed <= len(results) else list(results)

    merged_sources: list[dict] = []
    by_call: dict[str, list[dict]] = {}
    for r in fresh:
        by_call.setdefault(str(r.get("call_id") or ""), []).append(r)
        for t in r.get("tool_trace") or []:
            entry = dict(t)
            entry["step"] = len(state["tool_trace"]) + 1
            state["tool_trace"].append(entry)

    for call_id, group in by_call.items():
        # 来源重编号：子代理各自从 0 起编，必须换成**全局编号**再给主 agent，
        # 否则回报里的 [n] 与来源卡片对不上（前端按全局 index 解析引用）
        offset = 0
        group_sources = _renumber_subagent_sources(group, counter)
        merged_sources.extend(group_sources)
        payload = []
        for r in group:
            count = len(r.get("sources") or [])
            indices = [
                s.get("index") for s in group_sources[offset : offset + count]
            ]
            offset += count
            payload.append(
                {
                    "task": r.get("task"),
                    "mode": r.get("mode"),
                    "summary": r.get("summary") or "",
                    "artifacts": r.get("artifacts") or [],
                    "commands": r.get("commands") or [],
                    "files_read": r.get("files_read") or [],
                    "verify": r.get("verify") or None,
                    "sources": indices,
                }
            )
        content = json.dumps(
            {
                "results": payload,
                "note": (
                    "子代理自述，未经核实；artifacts 里的 action=created 才是"
                    "本子任务新建的文件，modified 表示改动了既有文件。"
                ),
            },
            ensure_ascii=False,
        )
        messages.append(
            ToolMessage(
                content=content,
                tool_call_id=call_id,
                name=DISPATCH_TOOL_NAME,
            )
        )
        for r in group:
            bus.emit(
                "tool_result",
                {
                    "id": f"subtask_{counter[0]}_{len(state['tool_trace'])}",
                    "name": "subagent",
                    "summary": str(r.get("summary") or "")[:160],
                    "duration_ms": None,
                    "sources": r.get("sources") or [],
                    "artifacts": r.get("artifacts") or [],
                },
            )
    if not by_call:
        # 异常路径（声明为空）：给一条提示避免图停摆
        messages.append(
            SystemMessage(content="（本轮派发声明没有产生任何子任务。）")
        )

    state["sources"].extend(merged_sources)

    # 任务清单：把已完成子任务勾掉（声明时已 append 成 plan 项）
    todos = state.get("todos") or []
    if db is not None and runtime.get("conv_id") and fresh:
        try:
            texts = [
                _todo_label(r.get("task")) for r in fresh if r.get("task")
            ]
            if texts:
                updated = complete_steps_by_text(db, runtime["conv_id"], texts)
                if updated:
                    todos = updated
            else:
                todos = load_todos(db, runtime["conv_id"]) or todos
            state["todos"] = todos
            done, total, remaining = plan_progress(todos)
            state["plan_done_count"] = done
            bus.emit("todos", {"todos": todos})
            if total:
                bus.emit(
                    "plan_progress",
                    {
                        "done": done,
                        "total": total,
                        "current": (
                            remaining[0].get("text") if remaining else None
                        ),
                        "text": f"计划进度：已完成 {done}/{total} 步（子任务已返回）。",
                    },
                )
        except Exception as exc:
            logger.warning("合并后同步任务清单失败：%s", exc)

    next_round = int(state.get("dispatch_rounds") or 0) + 1
    return {
        "messages": messages,
        "tool_trace": state["tool_trace"],
        "sources": state["sources"],
        "plan_done_count": state.get("plan_done_count", 0),
        "todos": state.get("todos") or [],
        # 声明已消费：清空后若模型再声明，条件边会再派一轮（多轮派发）
        "pending_subtasks": [],
        "dispatch_rounds": next_round,
        "subagent_consumed": len(results),
        "subagent_round_count": next_round,
    }


# ============================================================
# 节点：agent（ReAct 思考 + 流式生成）
# ============================================================



SUBAGENT_SYSTEM_PROMPT = (
    "你是主 Agent 派出的子任务代理。只完成分配给你的这一个子任务："
    "必要时调用可用工具，然后输出该子任务的结论摘要（2~4 句，宁可具体不要空泛）。\n"
    "主 Agent 除摘要与产物证据外看不到你的工具原始输出，所以：\n"
    "1. 保留关键事实、数字、专有名词与来源（文件名/页码/链接），不要只给笼统概括；\n"
    "2. 检索不到或无法确认的，明确写「未找到」「不确定」，不要用推测填补；\n"
    "3. 只陈述与本子任务相关的结论，不要输出最终答案、不要调用无关工具。\n"
    "4. 任务要求产出文件时，**产物必须由你自己在本子任务中创建或修改**。"
    "若发现同名/同题的既有文件，必须在摘要里明确写成「历史遗留产物（非本子任务"
    "产出）」，绝不能把它当作已完成的结果上报；用户要的是新产出，凭旧文件"
    "交差比如实说明「没做」更糟。\n"
    "5. 写完后如实报告：改动了哪些文件（路径）、验证命令与结果；未产出就直说。\n"
    "写文件/执行命令等敏感操作会由系统请求用户确认，被拒绝时如实说明影响"
    "并询问替代方案。"
)


