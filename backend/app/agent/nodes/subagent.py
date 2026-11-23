"""子代理域：dispatch/subagent/merge 节点与辅助（Send 并行、受限工具、结论合并，自 langgraph_agent.py 拆分）。"""

from __future__ import annotations

import json
import logging
import re
import threading
import time

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import Send
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...llm_text import extract_thinking_blocks, message_text
from ...prompt_safety import UNTRUSTED_NOTE
from ...runtime_config import effective, effective_or

logger = logging.getLogger(__name__)
from ..state import AgentState, EventBus
from ..utils import (
    _XML_TOOL_MARKERS,
    _normalize_tool_markers,
    _parse_xml_tool_calls,
    _run_verify,
    _slim_tool_result,
    tool_content_limit,
)
from .prompts import EMPTY_RETRY_HINT, XML_RETRY_HINT, hook_context_notice

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
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

# 模式常量/工具白名单/提示词已迁到 app/agent/agent_types.py（2026-11-21 6.1），
# 这里保留原私有名做兼容别名：既有导入路径与测试不用改。
from ..agent_types import (  # noqa: E402  (常量区，保持下游定义顺序)
    ALL_SUBAGENT_TOOLS,
    EXECUTE_TOOLS as _EXECUTE_TOOLS,
    MODE_ALIASES as _MODE_ALIASES,
    MODE_EXECUTE,
    MODE_EXPLORE,
    MODE_LABELS as _MODE_LABELS,
    MODE_PACK_HINTS as _MODE_PACK_HINTS,
    MODE_READ_ONLY as _MODE_READ_ONLY,
    MODE_RESEARCH,
    MODE_REVIEW,
    MODE_TOOLS as _MODE_TOOLS,
    READ_FS_TOOLS as _READ_FS_TOOLS,
    RESEARCH_TOOLS as _RESEARCH_TOOLS,
    SUBAGENT_BASE_PROMPT,
    SUBAGENT_MODE_PROMPTS,
    mode_system_prompt as subagent_system_prompt,
    mode_tools as _mode_tools,
    normalize_mode as _normalize_mode,
    resolve_agent_profile,
)

_MAX_SUBTASKS_PER_ROUND = 4
_EXECUTE_ALIASES = {"execute", "write", "bash", "command", "run"}

DISPATCH_ARGS_EXAMPLE = 'dispatch_subtasks({"task": "...", "mode": "explore|research|review|execute", "profile": "可选类型"})'

_DISPATCH_DESCRIPTION = (
    "把自包含子任务交给并行子代理（每轮≤4）。简单并行请直接调工具；"
    "仅当分支需多轮循环、深读或独立验收时用。\n"
    "mode=explore 只读侦察；research 只读检索（知识库/联网）；"
    "review 只读审查（严重度+file:line）；execute 写/命令（每轮只放开一个，其余降级）。\n"
    "task 必须自包含（子代理看不到对话历史）：目标+产出物路径+验收标准+禁止事项；"
    "回报是子代理自述+产物证据，未经核实。"
)


class _DispatchArgs(BaseModel):
    """dispatch_subtasks 的参数（结构化约束比自由文本可靠）。"""

    task: str = Field(
        description=(
            "自包含任务书：目标+动机+产出物/路径+验收标准+禁止事项。"
            "子代理看不到对话历史，必须把相关结论与硬约束写进来。"
        )
    )
    mode: str = Field(
        default=MODE_RESEARCH,
        description=(
            "explore=只读侦察；research=只读检索；"
            "review=只读审查；execute=写/命令（每轮≤1）"
        ),
    )
    profile: str = Field(
        default="",
        description="可选子代理类型名（设置页定义）；留空按 mode 默认。",
    )


def make_dispatch_tool(settings) -> BaseTool:
    """声明式派发工具的**定义**（不执行副作用，由编排层扇出）。

    仅供 bind_tools 使用：agent 节点会把它从 pending_tool_calls 里摘出来放进
    pending_subtasks，由图条件边走 Send 并行——若与普通工具混在同一轮，
    只受理声明并提示模型下一轮重发其余调用。
    """

    def _invoke(task: str, mode: str = MODE_RESEARCH, profile: str = "") -> dict:
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
    tool_call 一一对应地回填 ToolMessage（协议要求一个 call 一条结果）；
    subtask_id 是运行内的稳定 id（与前端轨迹 entry、分支取消注册表共用）。
    """
    subtasks: list[dict] = []
    for tc in declared_calls:
        args = (tc or {}).get("args") or {}
        call_id = str((tc or {}).get("id") or "")
        nested = args.get("subtasks") if isinstance(args.get("subtasks"), list) else None
        raw = nested if nested is not None else [args]
        base_profile = str(args.get("profile") or "").strip().lower()[:64]
        for item in raw:
            if len(subtasks) >= _MAX_SUBTASKS_PER_ROUND:
                break
            profile = base_profile
            if isinstance(item, str):
                task, mode = item.strip(), MODE_RESEARCH
            elif isinstance(item, dict):
                task = str(item.get("task") or item.get("step") or "").strip()
                mode = str(item.get("mode") or MODE_RESEARCH).strip().lower()
                profile = str(item.get("profile") or base_profile).strip().lower()[:64]
            else:
                continue
            if not task:
                continue
            mode = _normalize_mode(mode)
            subtasks.append(
                {
                    "task": task[:400],
                    "mode": mode,
                    "profile": profile,
                    "call_id": call_id,
                    # 前端轨迹 entry / 分支取消注册表 / 三件套 SSE 事件共用这个 id
                    "subtask_id": f"subtask_{call_id or 'call'}_{len(subtasks)}",
                }
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


def _subtask_ui_id(task: dict | None, call_id: str = "") -> str:
    """子任务在前端轨迹 / 取消注册表里的稳定 id。

    新数据用声明时分配的 subtask_id；旧数据（测试/历史结果）回退到
    "call_id + 任务文本哈希"——与 6.7 之前的轨迹 id 格式保持一致。
    """
    task = task or {}
    explicit = str(task.get("subtask_id") or "")
    if explicit:
        return explicit
    cid = str(task.get("call_id") or call_id or "")
    return f"subtask_{cid}_{abs(hash(str(task.get('task') or ''))) % 10**6}"


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


_CITATION_RE = re.compile(r"\[(\d{1,3})\]")


def _remap_citation_indices(summary: str, mapping: dict[int, int]) -> str:
    """按局部→全局编号映射改写摘要里的 [n]。

    子代理各自从 1 起编号 sources，摘要正文用 [n] 引用；merge 重编号后
    必须同步改写正文，否则多分支并行时主 agent 看到的引用会指向别的来源。
    映射里没有的编号原样保留（可能是子代理引用了历史对话里的编号）。
    """
    if not summary or not mapping:
        return summary

    def _sub(match: re.Match) -> str:
        target = mapping.get(int(match.group(1)))
        return f"[{target}]" if target is not None else match.group(0)

    return _CITATION_RE.sub(_sub, summary)


def _reserve_subagent_llm_call(deps, limit: int) -> bool:
    """从本次运行的共享预算里预留一次子代理 LLM 调用。

    Deps 是请求级活对象（不进检查点），并行 Send 分支拿到的是同一个实例；
    计数与锁放这里，state 里只放可序列化数据。
    """
    lock = getattr(deps, "subagent_lock", None)
    if lock is None:
        return True
    if limit <= 0:
        return False
    with lock:
        used = int(getattr(deps, "subagent_llm_calls", 0) or 0)
        if used >= limit:
            return False
        deps.subagent_llm_calls = used + 1
        return True


def _type_budget_allows(limit, used: int) -> bool:
    """类型级预算是否还有余量（空/0 = 不单独限制，只受共享预算约束）。"""
    if not limit:
        return True
    return int(used) < int(limit)


def _usage_delta(before: dict | None, after: dict | None) -> dict:
    """本分支的用量增量（线程级 aux 收集器是累计值，取差才属于本分支）。"""
    keys = ("llm_calls", "prompt_tokens", "completion_tokens", "total_tokens")
    return {
        key: max(
            0,
            int((after or {}).get(key) or 0) - int((before or {}).get(key) or 0),
        )
        for key in keys
    }


def _subagent_tools(
    service: "LangGraphAgentService",
    mode: str,
    counter: list[int],
    project_dir: str | None = None,
    sandbox: str | None = None,
    conv_id: int | None = None,
):
    """按执行模式给子代理构建工具集。

    - research：只读检索（知识库/联网/列目录/读文件/搜索）——可安全并行；
    - explore/review：只读侦察/审查（文件与代码，不含知识库/联网/写）；
    - execute：research + 写文件/编辑/删除/命令（敏感操作走 HITL 确认）。

    子代理看不到主对话，按名加载技能（skill_view，只读）能拿到完整步骤；
    执行脚本仍要回到主代理走审批。
    """
    mode = _normalize_mode(mode)
    from ...tools_extra import (
        make_bash_tool,
        make_delete_file_tool,
        make_edit_file_tool,
        make_grep_search_tool,
        make_list_dir_tool,
        make_read_file_tool,
        make_write_file_tool,
    )
    from ..tools import (
        make_knowledge_base_tool,
        make_skill_view_tool,
        make_web_search_tool,
    )

    settings = service.settings
    try:
        from ...skills import _get_cached_prefs

        enabled_ids = set((_get_cached_prefs() or {}).get("enabled") or [])
    except Exception:
        enabled_ids = None
    tools: list = []
    if mode in (MODE_RESEARCH, MODE_EXECUTE):
        tools.extend(
            [
                make_knowledge_base_tool(
                    service.rag,
                    counter,
                    bool(effective(settings, "crag_fallback_enabled")),
                    float(effective(settings, "crag_min_score") or 0.45),
                    effective(settings, "web_search_provider"),
                    effective(settings, "tavily_api_key") or "",
                    effective(settings, "web_search_max_results"),
                    allow_web_fallback=False,
                    searxng_base_url=effective(settings, "searxng_base_url") or "",
                    searxng_engines=effective(settings, "searxng_engines") or "",
                ),
                make_web_search_tool(
                    effective(settings, "web_search_provider"),
                    effective(settings, "tavily_api_key") or "",
                    effective(settings, "web_search_max_results"),
                    counter,
                    searxng_base_url=effective(settings, "searxng_base_url") or "",
                    searxng_engines=effective(settings, "searxng_engines") or "",
                ),
            ]
        )
    tools.extend(
        [
            make_list_dir_tool(settings, project_dir),
            make_read_file_tool(settings, project_dir),
            make_grep_search_tool(settings, project_dir),
            make_skill_view_tool(
                service.rag,
                enabled_ids=enabled_ids,
                conv_id=conv_id,
                full_max_chars=int(
                    effective_or(settings, "skills_full_view_max_chars", 24000) or 24000
                ),
                dedup=bool(effective_or(settings, "skills_view_dedup", True)),
                usage=bool(effective_or(settings, "skills_usage_tracking", True)),
            ),
        ]
    )
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
    tool_names: list[str] | tuple[str, ...] | None = None,
    task: str = "",
) -> str:
    """子代理的上下文包：工作目录/工具/模式提示 + 顶层清单 + git/清单/关键词/摘要。

    子代理看不到主对话，只能靠这里的既有事实作判断。文件清单带修改时间是
    刻意的——2026-09-24 事故里子代理把磁盘上的历史遗留产物当成了本子任务
    成果，时间戳让"这不是本轮产出的东西"变得可判。

    6.4 起附加 git status/diff、项目清单、任务关键词命中文件与近期摘要，
    总量 3000 字符封顶；各段探测失败只记 warning，不拖垮子任务。
    """
    from pathlib import Path

    from ...tools_extra import _resolve_workspace
    from ..context_pack import build_context_pack

    try:
        workspace = _resolve_workspace(service.settings, state.get("project_dir"))
    except Exception as exc:  # 目录不可达不该拖垮子任务
        logger.warning("子代理上下文包解析工作目录失败：%s", exc)
        workspace = Path.cwd()
    if not task:
        task = str((state.get("sub_task") or {}).get("task") or "")
    return build_context_pack(
        workspace,
        task=task,
        mode=mode,
        tool_names=tool_names,
        summary_text=state.get("summary_text") or state.get("memory_summary"),
    )


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


def _dispatch_node(state: AgentState, runtime) -> dict:
    """受理声明：把子任务落进任务清单（硬约束的种子），并升格任务态预算。

    真正的 Send 扇出由条件边函数 _dispatch_tasks 发出；这里只做落库与状态同步。
    第二参数是 LangGraph 按名注入的 Runtime（请求级依赖取 `runtime.context`）。
    """
    from ...todos import append_plan_todos, plan_progress

    deps = runtime.context
    subtasks = state.get("pending_subtasks") or []
    bus: EventBus | None = deps.bus
    db: Session | None = deps.db
    rt: dict = state.get("runtime") or {}
    steps: list[str] = []
    for task in subtasks:
        label = _todo_label(task.get("task"))
        if label and label not in steps:
            steps.append(label)

    # 声明就是本轮的工作清单：追加进 todos，硬约束据此生效
    # （无 todos 时 agent.py 的计划硬约束恒不触发，声明轮之后就成了"无约束"）
    todos = state.get("todos") or []
    if db is not None and rt.get("conv_id") and steps:
        try:
            todos = append_plan_todos(db, rt["conv_id"], steps) or todos
        except Exception as exc:
            logger.warning("子任务落清单失败：%s", exc)
    done, total, remaining = plan_progress(todos)
    if bus is not None:
        if subtasks:
            bus.emit(
                "status",
                {
                    "phase": "subagent",
                    "text": f"正在并行处理 {len(subtasks)} 个子任务…",
                },
            )
        for task in subtasks:
            sid = _subtask_ui_id(task)
            bus.emit(
                "tool_start",
                {
                    "id": sid,
                    "name": "subagent",
                    "arguments": {
                        "task": str(task.get("task") or "")[:120],
                        "mode": task.get("mode"),
                        "profile": task.get("profile") or None,
                        # 取消入口按这个 id 调 /agent/cancel/{run_id}/subagent/{id}
                        "subtask_id": sid,
                    },
                },
            )
        if any(len(str(t.get("task") or "").strip()) < 12 for t in subtasks):
            bus.emit(
                "status",
                {
                    "phase": "subagent",
                    "text": "有子任务描述过短，建议补齐目标/产出/验收标准。",
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
        # Send 的 payload 就是该节点看到的 state，所以这里显式带齐 merge 要用的键
        # ——「merge 必读键 ⊆ 本载荷」由 test_subagent_merge.py 的契约测试守住。
        logger.warning("派发声明为空，跳过扇出直接合并")
        return [
            Send(
                "merge",
                {
                    "runtime": state["runtime"],
                    "question": state.get("question"),
                    "messages": state.get("messages") or [],
                    "sources": state.get("sources") or [],
                    "counter": state.get("counter") or [0],
                    "tool_trace": state.get("tool_trace") or [],
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
                "question": state["question"],
                "runtime": state["runtime"],
                "project_dir": state.get("project_dir"),
                # 计划模式是全局只读约束：分支 state 必须继承，否则 execute
                # 子任务会在 allow 模式下绕过主流程的只读拦截
                "plan_only": bool(state.get("plan_only")),
                # 子代理 skill_view 的用量归因需要会话号（Send 载荷即分支 state）
                "conversation_id": state.get("conversation_id"),
            },
        )
        for task in subtasks
    ]


def _subagent_sensitive_gate(
    service: "LangGraphAgentService",
    name: str,
    args: dict,
    invoke_fn,
    tool,
    bus,
    rt: dict,
) -> dict:
    """子代理敏感操作闸门：白名单/allow 模式直接执行，其余交回主代理。

    子代理跑在并行 Send 分支里，无法 interrupt 向用户提问（该分支的恢复语义
    未经验证且牵动 merge），所以这里不开放阻塞式审批：需要写/命令时由主代理
    自己执行（走主流程的原生挂起审批）。
    """
    from ...command_risk import high_risk_command, high_risk_file_operation
    from ...permissions import describe_tool_call, get_permission_manager
    from ...tools_extra import command_allowed

    settings = service.settings
    command = str(args.get("command") or "")
    risk_reason = (
        high_risk_command(command)
        if name in ("bash", "command_tool", "bash_background") and command
        else high_risk_file_operation(name, args)
    )
    if risk_reason:
        # 高危命令/高危路径即使是 allow 模式也不能由子代理静默执行，必须交回主代理走确认
        summary = describe_tool_call(name, args)
        if bus is not None:
            bus.emit(
                "status",
                {
                    "phase": "permission",
                    "text": f"子代理请求高危操作（{risk_reason}），已交回主代理：{summary}",
                },
            )
        return {
            "summary": f"子代理不能执行高危操作：{summary}",
            "permission": "subagent_denied",
            "error": (
                f"该操作被判定为高危（{risk_reason}），子代理不执行。"
                "请把这一步交回主代理执行（主代理会向用户发起强制确认）。"
            ),
        }
    if effective(settings, "tool_permission_mode") == "allow":
        return invoke_fn(tool, args)
    if name in ("bash", "command_tool", "bash_background") and (
        command_allowed(settings, command)[0]
        or get_permission_manager().is_session_allowed(rt.get("conv_id"), command)
    ):
        return invoke_fn(tool, args)

    summary = describe_tool_call(name, args)
    if bus is not None:
        bus.emit(
            "status",
            {"phase": "permission", "text": f"子代理请求敏感操作，已交回主代理：{summary}"},
        )
    return {
        "summary": f"子代理不能执行敏感操作：{summary}",
        "permission": "subagent_denied",
        "error": (
            f"该操作需要人工确认（{summary}），子代理不开放敏感操作。"
            "请把这一步交回主代理执行（主代理会向用户发起确认）。"
        ),
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
    elif name in ("bash", "command_tool", "bash_background"):
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


def _subagent_node(state: AgentState, runtime) -> dict:
    """一个子代理分支：独立 messages + 受限工具，敏感操作走 HITL，
    返回结论摘要 + 产物证据。第二参数是 LangGraph 按名注入的 Runtime。"""
    from ...permissions import (
        describe_tool_call,
        display_args,
        get_permission_manager,
        is_sensitive_tool,
    )
    from ...tools_extra import command_allowed
    from ..tools import extract_sources

    deps = runtime.context
    service: LangGraphAgentService = deps.service
    settings = service.settings
    bus: EventBus | None = deps.bus
    rt: dict = state.get("runtime") or {}
    question = state.get("question") or ""
    task = state.get("sub_task") or {}
    step = str(task.get("task") or task.get("step") or "")
    raw_mode = str(task.get("mode") or MODE_RESEARCH)
    spec = resolve_agent_profile(str(task.get("profile") or ""), raw_mode)
    mode = spec["mode"]
    if state.get("plan_only") and not spec["read_only"]:
        # 计划模式只读：即使模型声明了 execute（或自定义可写类型），也只装配只读工具
        spec = resolve_agent_profile("", MODE_RESEARCH)
        mode = spec["mode"]
    call_id = str(task.get("call_id") or "")
    subtask_id = _subtask_ui_id(task, call_id)
    stop_event = deps.stop_event
    # 6.7：每个 Send 分支有自己的取消信号；整轮停止时令牌会把分支一并置位
    branch_event: threading.Event | None = None
    register_branch = getattr(stop_event, "register_branch", None)
    if callable(register_branch):
        branch_event = register_branch(subtask_id)

    def _stopped() -> bool:
        """整轮停止或本分支被单独取消（协作式，在循环检查点生效）。"""
        return (stop_event is not None and stop_event.is_set()) or (
            branch_event is not None and branch_event.is_set()
        )

    counter: list[int] = [0]
    all_tools = _subagent_tools(
        service, mode, counter, state.get("project_dir"),
        sandbox=rt.get("command_sandbox"),
        conv_id=state.get("conversation_id"),
    )
    allowed_tools = set(spec["tools"])
    tools = [t for t in all_tools if t.name in allowed_tools]

    pack = _build_context_pack(service, state, mode, spec["tools"], task=step)
    mode_label = _MODE_LABELS.get(mode, mode)
    messages = [
        SystemMessage(content=spec["system_prompt"]),
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
    permission_manager = get_permission_manager()
    research_rounds = max(
        1, int(effective(settings, "agent_subagent_max_rounds") or 2)
    )
    if mode == MODE_EXECUTE:
        # 执行类至少要 写→验→修 三轮，2 轮根本做不完（旧配置一刀切 2 轮）
        max_rounds = max(
            1, int(effective(settings, "agent_subagent_exec_max_rounds") or 6)
        )
    elif mode == MODE_REVIEW:
        # 审查需要多看几处证据，但不必像执行类那么长
        max_rounds = max(research_rounds, 3)
    else:
        max_rounds = research_rounds
    if spec.get("max_rounds"):
        # 类型级轮数覆盖（6.1）：配置值优先于 mode 默认
        max_rounds = max(1, int(spec["max_rounds"]))

    def _invoke(tool, args: dict) -> dict:
        try:
            out = tool.invoke(args)
            return out if isinstance(out, dict) else {"result": out}
        except Exception as exc:
            return {"error": str(exc)}

    # hooks 快照：节点入口读一次，分支内只读内存（load_hooks 自带短会话）
    from ...hooks import load_hooks, run_hooks_from

    hooks_snapshot: list[dict] = []
    if deps.db is not None:
        try:
            hooks_snapshot = load_hooks(deps.db)
        except Exception as exc:
            logger.warning("子代理加载 hooks 失败：%s", exc)

    llm_budget = int(effective(settings, "agent_subagent_max_llm_calls") or 24)
    type_budget = spec.get("llm_budget")
    type_calls = 0
    budget_exhausted = False
    error_text = ""

    def _reserve() -> bool:
        """类型级预算 + 共享预算双重检查（6.2）。

        类型预算为空 = 只受共享预算约束；设置了就同时限制本子任务，避免
        一个宽泛任务吃光整轮 24 次调用。
        """
        nonlocal type_calls
        if not _type_budget_allows(type_budget, type_calls):
            return False
        if not _reserve_subagent_llm_call(deps, llm_budget):
            return False
        type_calls += 1
        return True

    def _resolve_model(thinking_off: bool):
        resolver = getattr(service, "subagent_chat_model", None)
        if resolver is None:  # 测试替身/旧服务：回退主模型
            return service.chat_plain if thinking_off else service.chat
        return resolver(
            spec.get("model_provider_id") or "",
            spec.get("model") or "",
            spec.get("thinking") or "inherit",
            thinking_off,
        )

    def _call_model(thinking_off: bool):
        """预留预算后调用模型；预算耗尽返回 None。"""
        if not _reserve():
            return None
        model = _resolve_model(thinking_off)
        chat = model.bind_tools(tools) if tools else model
        # 信号量只覆盖 LLM 请求本身：工具 I/O 期间不占并发名额，
        # 否则并行 Send 的子代理会互相饿死。
        service.rag.acquire_llm()
        try:
            return chat.invoke(
                messages,
                config={"callbacks": [get_aux_usage_collector()]},
            )
        finally:
            service.rag.release_llm()

    t0 = time.perf_counter()
    try:
        _resolved = _resolve_model(False)
        model_name = str(
            getattr(_resolved, "model_name", None)
            or getattr(_resolved, "model", None)
            or spec.get("model")
            or ""
        )
    except Exception:
        model_name = spec.get("model") or ""
    if bus is not None:
        # 6.7 观测：分支真正开始执行（dispatch 阶段的 tool_start 只是占位卡）
        bus.emit(
            "subagent_start",
            {
                "subtask_id": subtask_id,
                "call_id": call_id,
                "task": step[:160],
                "mode": mode,
                "profile": spec.get("profile") or "",
                "type": spec["key"],
                "model": model_name,
                "max_rounds": max_rounds,
            },
        )
    try:
        usage_before = get_aux_usage_collector().summary()
    except Exception:
        usage_before = {}
    try:
        rounds_used = 0
        empty_retry_left = 1
        xml_retry_left = 1
        use_plain = False
        while rounds_used < max_rounds:
            if _stopped():
                break
            rounds_used += 1
            if bus is not None:
                bus.emit(
                    "subagent_progress",
                    {
                        "subtask_id": subtask_id,
                        "call_id": call_id,
                        "phase": "round",
                        "round": rounds_used,
                        "max_rounds": max_rounds,
                    },
                )
            # 空正文重试只在紧接的一轮生效（与主链 empty_retry_pending 同语义）
            thinking_off = use_plain
            use_plain = False
            resp = _call_model(thinking_off)
            if resp is None:
                budget_exhausted = True
                break
            if _stopped():
                # 取消/停止发生在 LLM 调用期间：本轮请求的工具不再执行
                break
            tool_calls = list(getattr(resp, "tool_calls", None) or [])
            merged_text = message_text(resp.content)
            has_xml_marker = False
            if not tool_calls:
                normalized = _normalize_tool_markers(merged_text)
                has_xml_marker = any(m in normalized for m in _XML_TOOL_MARKERS)
                if has_xml_marker:
                    # XML 风格工具调用兜底：重建 AIMessage，保证 tool_calls 与
                    # 随后 ToolMessage 的 id 配对（与主链同一套解析）
                    parsed = _parse_xml_tool_calls(normalized)
                    if parsed:
                        extra = dict(
                            getattr(resp, "additional_kwargs", None) or {}
                        )
                        thinking = extract_thinking_blocks(
                            getattr(resp, "content", None)
                        )
                        if thinking:
                            extra["anthropic_thinking"] = thinking
                        resp = AIMessage(
                            content="", tool_calls=parsed, additional_kwargs=extra
                        )
                        tool_calls = parsed
                        merged_text = ""
                        has_xml_marker = False
            messages.append(resp)
            if not tool_calls:
                if (
                    has_xml_marker
                    and xml_retry_left > 0
                    and not _stopped()
                ):
                    xml_retry_left -= 1
                    rounds_used -= 1
                    messages.append(SystemMessage(content=XML_RETRY_HINT))
                    continue
                usage = getattr(resp, "usage_metadata", None)
                output_tokens = int(((usage or {}).get("output_tokens")) or 0)
                # 部分 OpenAI 兼容网关不回填 usage：此时无法区分"被长度截断"
                # 与"真空响应"，仍给一次有界重试（重试便宜且次数受限）
                usage_unknown = not usage
                if (
                    not merged_text.strip()
                    and (usage_unknown or output_tokens > 0)
                    and empty_retry_left > 0
                    and not _stopped()
                ):
                    # 思考吃光输出预算：不消耗工具轮，下一轮换无思考模型
                    empty_retry_left -= 1
                    use_plain = True
                    rounds_used -= 1
                    messages.append(SystemMessage(content=EMPTY_RETRY_HINT))
                    continue
                summary = merged_text
                break
            by_name = {t.name: t for t in tools}
            hook_contexts: list[str] = []
            for tc in tool_calls:
                name = tc.get("name")
                tool = by_name.get(name)
                args = tc.get("args") or {}
                if bus is not None:
                    # 6.7 观测：分支当前在调什么工具（不携带完整参数，避免把
                    # 文件内容/命令原文推进事件流）
                    try:
                        hint = describe_tool_call(name, args)[:160]
                    except Exception:
                        hint = ""
                    bus.emit(
                        "subagent_progress",
                        {
                            "subtask_id": subtask_id,
                            "call_id": call_id,
                            "phase": "tool",
                            "tool": name,
                            "hint": hint,
                            "round": rounds_used,
                        },
                    )
                if tool is None:
                    result: dict = {"error": f"子代理不可用工具：{name}"}
                else:
                    # PreToolUse hooks：子代理同样受用户 hook 约束
                    denied = [
                        r
                        for r in run_hooks_from(
                            hooks_snapshot, "pre_tool_use", name, args
                        )
                        if r.get("decision") == "deny"
                    ]
                    if denied:
                        reason = "；".join(
                            r.get("reason") or "被 hook 拒绝" for r in denied
                        )
                        result = {
                            "summary": f"工具被 PreToolUse hook 拦截：{reason}",
                            "permission": "hook_denied",
                            "error": (
                                f"操作被 hook 拒绝：{reason}。"
                                "请说明影响并询问替代方案。"
                            ),
                        }
                    elif is_sensitive_tool(name, args):
                        result = _subagent_sensitive_gate(
                            service,
                            name,
                            args,
                            _invoke,
                            tool,
                            bus,
                            rt,
                        )
                    else:
                        result = _invoke(tool, args)
                    for hook_result in run_hooks_from(
                        hooks_snapshot,
                        "post_tool_use",
                        name,
                        {"tool_input": args, "tool_result": result},
                    ):
                        if hook_result.get("additional_context"):
                            hook_contexts.append(
                                f"[{hook_result['name']}] "
                                f"{hook_result['additional_context']}"
                            )
                # 进历史的工具结果瘦身（阈值按工具类型分级，见 tool_content_limit）
                slim = _slim_tool_result(
                    result, content_limit=tool_content_limit(name)
                )
                messages.append(
                    ToolMessage(
                        content=json.dumps(slim, ensure_ascii=False),
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
            if hook_contexts:
                # 必须等本轮所有 ToolMessage 入链后再插，避免破坏
                # Anthropic 的 tool_use / tool_result 相邻要求
                messages.append(
                    SystemMessage(content=hook_context_notice(hook_contexts))
                )
        if (
            not summary
            and not budget_exhausted
            and not _stopped()
        ):
            # 兜底：轮数用尽仍在调工具时，强制补一次结论摘要，保证主 Agent 拿到结果
            if _reserve():
                try:
                    messages.append(
                        SystemMessage(
                            content=(
                                "请基于上面的工具结果，用 2~4 句话输出"
                                "本子任务的结论摘要。\n"
                                + UNTRUSTED_NOTE
                            )
                        )
                    )
                    final_model = _resolve_model(False)
                    service.rag.acquire_llm()
                    try:
                        final_resp = final_model.invoke(
                            messages,
                            config={"callbacks": [get_aux_usage_collector()]},
                        )
                    finally:
                        service.rag.release_llm()
                    summary = message_text(final_resp.content)
                except Exception as exc:
                    summary = f"（子任务总结失败：{exc}）"
            else:
                budget_exhausted = True
    except Exception as exc:
        # 分支级故障隔离：一个子代理失败不能毁掉整轮 run；结果以结构化
        # error 回填给主代理（可据此重派或自己做）
        logger.warning("子代理执行失败（%s）：%s", mode, exc)
        error_text = f"{type(exc).__name__}: {exc}"
        if not summary:
            summary = f"（子任务执行失败：{error_text}）"

    branch_cancelled = bool(branch_event is not None and branch_event.is_set())
    if branch_cancelled and not summary:
        summary = "（用户已取消该子任务）"
    elif budget_exhausted and not summary:
        summary = "（子代理 LLM 调用预算已耗尽，本子任务未完成）"
    duration_ms = round((time.perf_counter() - t0) * 1000)
    try:
        tokens_used = _usage_delta(usage_before, get_aux_usage_collector().summary())
    except Exception:
        tokens_used = {}

    # 写后验证：子代理此前完全不校验产物，主循环却有 _run_verify。
    # 产物不可信时至少要让证据里出现"校验没过"。
    written = [
        str(a.get("path"))
        for a in artifacts
        if a.get("path") and a.get("action") in ("created", "modified")
    ]
    if written:
        try:
            verify_results = _run_verify(
                settings, written, state.get("project_dir")
            )
        except Exception as exc:  # 验证失败不该拖垮子任务
            logger.warning("子代理写后验证失败：%s", exc)

    # 清洗子代理输出中的 XML 工具调用标记，防止泄漏到主 agent 的 tool_result 摘要
    structured: dict = {
        "summary": summary,
        "findings": [],
        "open_questions": [],
        "structured": False,
    }
    if summary:
        import re as _re
        summary = _re.sub(r"</?tool_calls[^>]*>|</?invoke[^>]*>|</?parameter[^>]*>|</?tool_use[^>]*>|</?function[^>]*>", "", summary)
        summary = _re.sub(r"<\|", "<", summary)  # 清理 <user|tool_calls> 等变体
        # 结构化结果（6.3）：解析最后一个 json 围栏；失败无损回退原文
        from ..structured_result import parse_structured_result

        structured = parse_structured_result(summary)
        summary = _truncate_summary(structured["summary"])

    if not artifacts and mode == MODE_EXECUTE:
        # 执行类子任务却没产出任何文件：这是重要事实，必须显式汇报而不是让
        # 主 agent 从一段散文里自己猜（2026-09-24：子代理把旧文件当成果）。
        summary = (
            (summary or "")
            + "\n\n【产物证据】本子任务未创建或修改任何文件。"
        ).strip()

    if bus is not None:
        # 6.7 观测：分支收尾（merge 的 tool_result 仍会带完整来源/产物）
        bus.emit(
            "subagent_result",
            {
                "subtask_id": subtask_id,
                "call_id": call_id,
                "status": (
                    "cancelled"
                    if branch_cancelled
                    else "error"
                    if error_text
                    else "done"
                ),
                "mode": mode,
                "profile": spec.get("profile") or "",
                "type": spec["key"],
                "model": model_name,
                "rounds": rounds_used,
                "duration_ms": duration_ms,
                "tokens": tokens_used,
                "summary": (summary or "")[:200],
                "error": error_text or None,
                "findings": len(structured["findings"]),
            },
        )
    unregister_branch = getattr(stop_event, "unregister_branch", None)
    if callable(unregister_branch):
        unregister_branch(subtask_id, branch_event)

    return {
        "subagent_results": [
            {
                "name": step[:40],
                "task": step,
                "mode": mode,
                "profile": spec.get("profile") or "",
                "type_key": spec["key"],
                "model": model_name,
                "model_provider_id": spec.get("model_provider_id") or "",
                "call_id": call_id,
                "subtask_id": subtask_id,
                "cancelled": branch_cancelled,
                "rounds": rounds_used,
                "duration_ms": duration_ms,
                "tokens": tokens_used,
                "summary": summary or "（子任务未产出结论）",
                # 分支级失败也要成为硬事实，主代理可据此重派/自己重做
                "error": error_text or None,
                "artifacts": artifacts,
                "commands": commands,
                "files_read": files_read[:20],
                "verify": verify_results,
                "sources": sources_local,
                "tool_trace": trace_local,
                "findings": structured["findings"],
                "open_questions": structured["open_questions"],
                "structured": structured["structured"],
            }
        ]
    }


def _merge_node(state: AgentState, runtime) -> dict:
    """fan-in：把各子代理结果按 **tool_call 一一对应**回填成 ToolMessage。

    与旧实现（system 消息断言"（已完成）"）的关键差别：结果以**工具返回值**
    进入主 agent 上下文，它天然可以怀疑、重派、自己重做——从结构上拆掉了
    "低质结果被当成既成事实"的放大机制。第二参数是 LangGraph 注入的 Runtime。
    """
    from ...todos import complete_steps_by_text, load_todos, plan_progress

    deps = runtime.context
    bus: EventBus = deps.bus
    db: Session | None = deps.db
    rt: dict = state["runtime"]
    messages = state["messages"]
    results = state.get("subagent_results") or []
    counter = state["counter"]
    if bus is not None:
        bus.emit("status", {"phase": "subagent", "text": "正在汇合子任务结果…"})

    # 只消费本轮新增的部分：同一次派发分多轮时，前面的结果已在上一轮 merge 过。
    # 游标放 runtime（每轮新建的字典，不随 checkpointer 跨轮走）——通道本身
    # 已由 prepare 的 reset 归约保证每轮清空，跨轮残留问题从源头消除。
    consumed = int(rt.get("subagent_merged") or 0)
    fresh = results[consumed:] if 0 <= consumed <= len(results) else list(results)
    rt["subagent_merged"] = len(results)

    trace = state["tool_trace"]
    merged_sources: list[dict] = []
    by_call: dict[str, list[dict]] = {}
    for r in fresh:
        by_call.setdefault(str(r.get("call_id") or ""), []).append(r)
        for t in r.get("tool_trace") or []:
            entry = dict(t)
            entry["step"] = len(trace) + 1
            trace.append(entry)

    for call_id, group in by_call.items():
        # 来源重编号：子代理各自从 0 起编，必须换成**全局编号**再给主 agent，
        # 否则回报里的 [n] 与来源卡片对不上（前端按全局 index 解析引用）
        offset = 0
        group_sources = _renumber_subagent_sources(group, counter)
        merged_sources.extend(group_sources)
        payload = []
        # SSE 事件要发完整来源对象（前端按 index 渲染卡片）；模型侧 payload
        # 只发全局编号数组，两种形态不能混用（2026-10-23 修复）。
        shown: list[tuple[str, list[dict]]] = []
        for r in group:
            local_sources = r.get("sources") or []
            count = len(local_sources)
            remapped = group_sources[offset : offset + count]
            offset += count
            # 局部 → 全局编号映射：摘要正文里的 [n] 必须跟着改，否则引用错位
            citation_map: dict[int, int] = {}
            for local, global_src in zip(local_sources, remapped):
                try:
                    citation_map[int(local.get("index"))] = int(
                        global_src.get("index")
                    )
                except (TypeError, ValueError):
                    continue
            summary = _remap_citation_indices(r.get("summary") or "", citation_map)
            indices = [s.get("index") for s in remapped]
            shown.append((summary, remapped))
            payload.append(
                {
                    "task": r.get("task"),
                    "mode": r.get("mode"),
                    "profile": r.get("profile") or None,
                    "type": r.get("type_key"),
                    "model": r.get("model") or "",
                    "summary": summary,
                    "error": r.get("error") or None,
                    "artifacts": r.get("artifacts") or [],
                    "commands": r.get("commands") or [],
                    "files_read": r.get("files_read") or [],
                    "verify": r.get("verify") or None,
                    "sources": indices,
                    "findings": r.get("findings") or [],
                    "open_questions": r.get("open_questions") or [],
                    "structured": bool(r.get("structured")),
                    # 6.7：被单独取消的分支是硬事实，主代理可据此重派
                    "cancelled": bool(r.get("cancelled")),
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
        for (summary, source_items), r in zip(shown, group):
            # 与派发时的 tool_start 用同一 id：前端按 call_id 回填结果时能精确对上
            sid = _subtask_ui_id(r, call_id)
            verify = r.get("verify")
            if isinstance(verify, list):
                verify = verify[:10]
            detail = json.dumps(
                {
                    "artifacts": (r.get("artifacts") or [])[:20],
                    "commands": (r.get("commands") or [])[:10],
                    "files_read": (r.get("files_read") or [])[:20],
                    "verify": verify,
                    "rounds": r.get("rounds"),
                    "duration_ms": r.get("duration_ms"),
                    "profile": r.get("profile") or None,
                    "type": r.get("type_key"),
                    "model": r.get("model") or "",
                    "structured": bool(r.get("structured")),
                    "cancelled": bool(r.get("cancelled")),
                    "tokens": r.get("tokens") or None,
                    "findings": (r.get("findings") or [])[:10],
                    "open_questions": (r.get("open_questions") or [])[:5],
                    "sources": [
                        s.get("index") for s in source_items if isinstance(s, dict)
                    ][:10],
                },
                ensure_ascii=False,
            )[:3500]
            # 摘要轨迹条目让历史消息也能重建「子代理」页签；
            # 内部工具轨迹已在上面合并，这里只补一条运行级摘要
            entry = {
                "id": sid,
                "name": "subagent",
                "arguments": {
                    "task": str(r.get("task") or "")[:300],
                    "mode": r.get("mode"),
                    "profile": r.get("profile") or None,
                    "subtask_id": sid,
                },
                "summary": str(summary)[:400],
                "step": len(trace) + 1,
                "duration_ms": r.get("duration_ms"),
                "detail": detail,
            }
            trace.append(entry)
            bus.emit(
                "tool_result",
                {
                    "id": sid,
                    "name": "subagent",
                    "summary": str(summary)[:160],
                    "duration_ms": r.get("duration_ms"),
                    "sources": source_items,
                    "artifacts": r.get("artifacts") or [],
                    "detail": detail,
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
    if db is not None and rt.get("conv_id") and fresh:
        try:
            texts = [
                _todo_label(r.get("task")) for r in fresh if r.get("task")
            ]
            if texts:
                updated = complete_steps_by_text(db, rt["conv_id"], texts)
                if updated:
                    todos = updated
            else:
                todos = load_todos(db, rt["conv_id"]) or todos
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
        "subagent_round_count": next_round,
    }


# ============================================================
# 节点：agent（ReAct 思考 + 流式生成）
# ============================================================



# 提示词常量已迁到 app/agent/agent_types.py（6.1）；保留 research 版本文本别名，
# 兼容旧测试与外部导入。
SUBAGENT_SYSTEM_PROMPT = subagent_system_prompt(MODE_RESEARCH)


