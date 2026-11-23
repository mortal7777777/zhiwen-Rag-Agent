"""tools 节点：工具执行（并行/HITL/失败重试）、计划进度同步、checkpoint 保存（自 langgraph_agent.py 拆分）。"""

from __future__ import annotations

import json
import logging
import threading
import time

from sqlalchemy.orm import Session

from ...config import Settings
from ...runtime_config import effective, effective_or

logger = logging.getLogger(__name__)
from ..state import AgentState, EventBus
from ..tools import (
    ASK_QUESTION_TOOL_NAME,
    PLAN_ENTER_TOOL_NAME,
    PLAN_EXIT_TOOL_NAME,
    PLAN_MODE_SYSTEM_HINT,
)
from .common import (_bump_patch, _failure_limit, _iteration_limit, _plan_hint)
from .prompts import (
    PLAN_SUBMIT_HINT_FORCED,
    budget_exhausted_notice,
    failure_limit_notice,
    hook_context_notice,
    plan_confirmed_notice,
    plan_progress_notice,
    tool_failure_notice,
    verify_env_notice,
    verify_fail_notice,
    verify_pass_notice,
)
from ..utils import (
    _CACHEABLE_TOOLS,
    _run_verify,
    _slim_tool_result,
    _tool_cache_key,
    _tool_detail,
    _tool_has_required_args,
    tool_content_limit,
)

from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.types import interrupt  # 模块级引用：测试可 patch nodes.tools.interrupt
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..langgraph_agent import LangGraphAgentService  # 仅类型注解
from ..tools import extract_sources
from ..tool_preflight import validate_call
from ..tool_verification import augment_mutation

# ==== 函数体（原文）====
def _should_force_final(
    *,
    current: int,
    threshold: int,
    forced_final: bool,
    status: str | None,
) -> bool:
    """是否触发强制收尾：预算/失败数达阈值、未在收尾中、且未被手动停止。

    iterations（tool_calls_used vs 迭代上限）与 failures（failure_count vs
    失败上限）两个触发点共用同一形状的判定。
    """
    return current >= threshold and not forced_final and status != "stopped"


def _remember_command_forever(db: Session | None, command: str, settings: Settings) -> None:
    """「永久记住」：把命令原文写入自动放行前缀（命令白名单，设置页可见）。"""
    if db is None or not command:
        return
    from ...command_risk import high_risk_command

    if high_risk_command(command):
        logger.warning("拒绝把高危命令写入白名单：%s", command[:120])
        return
    try:
        from ...runtime_config import load_overrides, save_overrides

        load_overrides(db)
        current = effective(settings, "command_allowlist") or ""
        prefixes = [p.strip() for p in current.split(",") if p.strip()]
        if command not in prefixes:
            prefixes.append(command)
        save_overrides(db, {"command_allowlist": ", ".join(prefixes)})
    except Exception as exc:
        logger.warning("写入命令白名单失败：%s", exc)


def _tools_node(state: AgentState, runtime) -> dict:
    """第二参数是 LangGraph 按名注入的 Runtime；请求级依赖取 `runtime.context`。"""
    deps = runtime.context
    service: LangGraphAgentService = deps.service
    bus: EventBus = deps.bus
    settings: Settings = service.settings
    rt: dict = state["runtime"]
    messages = state["messages"]
    db: Session | None = deps.db
    stop_event = deps.stop_event
    from ...permissions import (
        COMMAND_TOOLS,
        FILE_EDIT_TOOLS,
        SENSITIVE_TOOLS,
        describe_tool_call,
        display_args,
        get_permission_manager,
        is_sensitive_tool,
    )
    from ...tools_extra import command_allowed
    from ...hooks import run_hooks

    permission_manager = get_permission_manager()

    tools_by_name = {t.name: t for t in deps.tools}
    tool_calls = state.get("pending_tool_calls") or []
    if not tool_calls:
        return {}
    verify_env_warned = bool(state.get("verify_env_warned"))

    # ---- 计划模式（只读）：敏感操作直接拦截，不弹审批（纯计算）----
    blocked: dict[str, str] = {}
    if state.get("plan_only"):
        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("args") or {}
            if is_sensitive_tool(name, args):
                blocked[tc.get("id")] = (
                    "当前为计划模式（只读）：不允许写文件/编辑/删除/执行命令。"
                    "请用只读手段把现状摸清，然后用 exit_plan_mode 提交结构化计划，"
                    "等待用户确认后再执行。"
                )

    # ---- 人工确认（HITL）：纯计算收集本轮待审批项 ----
    # 挂起恢复会整段重跑本节点：直到 interrupt() 之前不得有任何副作用
    # （发事件/写库/开线程）；白名单与会话记忆查询都是只读的。
    # 写/编辑/删除/命令默认 ask；命令命中自动放行白名单或本会话已记住则无需确认；
    # permission_mode=allow 时普通敏感操作自动批准，但高危命令是独立底线：
    # 无论 allow 还是白名单，都必须人工确认（见 command_risk）。
    from ...command_risk import high_risk_command, high_risk_file_operation

    ops: list[dict] = []
    permission_mode = effective_or(settings, "tool_permission_mode", "ask")
    # 请求级 --sandbox 覆盖优先；沙箱免确认只对 Docker 执行环境生效
    sandbox_mode = str(
        rt.get("command_sandbox")
        or effective(settings, "command_sandbox")
        or "subprocess"
    ).strip().lower()
    sandbox_auto_allow = sandbox_mode == "docker" and bool(
        effective_or(settings, "sandbox_auto_allow", False)
    )
    ops_by_command: dict[str, dict] = {}  # 同轮同命令去重：一次决定覆盖全部调用
    for tc in tool_calls:
        name = tc.get("name", "")
        args = tc.get("args") or {}
        if tc.get("id") in blocked:
            continue
        command = str(args.get("command") or "")
        risk_reason = (
            high_risk_command(command)
            if name in ("bash", "command_tool", "bash_background") and command
            else high_risk_file_operation(name, args)
        )
        if risk_reason:
            # 高危命令/高危文件路径：任何模式都必须人工确认
            needs = True
        elif permission_mode == "allow":
            needs = False
        elif sandbox_auto_allow and name in COMMAND_TOOLS and command:
            # Docker 沙箱（无网络、根文件系统只读）内运行，普通命令免确认
            needs = False
        elif permission_mode == "acceptedits" and name in FILE_EDIT_TOOLS:
            # 工作区内的文件写/改/删自动批准；命令、技能、知识库写入照常确认
            needs = False
        else:
            needs = is_sensitive_tool(name, args)
            if name in ("bash", "command_tool", "bash_background") and (
                command_allowed(settings, command)[0]
                or permission_manager.is_session_allowed(rt.get("conv_id"), command)
            ):
                needs = False
        if not needs:
            continue
        if command and command in ops_by_command:
            ops_by_command[command]["also_covers"].append(tc.get("id"))
            continue
        op = {
            "call_id": tc.get("id"),
            "also_covers": [],
            "name": name,
            "arguments": display_args(name, args),
            "summary": describe_tool_call(name, args),
            "risk": "high" if risk_reason else "",
            "risk_reason": risk_reason or "",
            # 只有命令类支持「记住」；高危命令永不提供，服务端也会忽略伪造的 remember
            "rememberable": name in ("bash", "command_tool", "bash_background") and not risk_reason,
        }
        ops.append(op)
        if command:
            ops_by_command[command] = op

    # ---- 计划提交（exit_plan_mode）：并入同一次挂起（纯计算）----
    # 仅 plan_only 时认 exit_plan_mode；该工具本身不执行，结果在恢复段合成。
    plan_request: dict | None = None
    if state.get("plan_only"):
        for tc in tool_calls:
            if tc.get("name") != PLAN_EXIT_TOOL_NAME or tc.get("id") in blocked:
                continue
            args = tc.get("args") or {}
            steps = [
                str(s).strip() for s in (args.get("steps") or []) if str(s).strip()
            ][:8]
            if steps:
                plan_request = {
                    "call_id": tc.get("id"),
                    "steps": steps,
                    "summary": str(args.get("summary") or "")[:200],
                }
                break

    # ---- 向用户提问（ask_user_question）：并入同一次挂起（纯计算）----
    # 与 exit_plan_mode 同模式：工具本身不执行，问题进 interrupt 载荷，
    # 恢复段按用户答案合成 ToolMessage；格式不合法则当场返回错误让模型修正。
    question_requests: list[dict] = []
    ask_result: dict[str, dict] = {}  # ask_user_question 的合成结果（_execute 直接取用）
    for tc in tool_calls:
        if tc.get("name") != ASK_QUESTION_TOOL_NAME:
            continue
        args = tc.get("args") or {}
        raw_questions = args.get("questions")
        cleaned: list[dict] = []
        for q in raw_questions if isinstance(raw_questions, list) else []:
            if not isinstance(q, dict):
                continue
            text = str(q.get("question") or "").strip()
            options: list[dict] = []
            for opt in q.get("options") or []:
                if not isinstance(opt, dict):
                    continue
                label = str(opt.get("label") or "").strip()[:24]
                if not label:
                    continue
                options.append(
                    {
                        "label": label,
                        "description": str(opt.get("description") or "").strip()[:120],
                    }
                )
                if len(options) >= 4:
                    break
            if not text or len(options) < 2:
                continue
            cleaned.append(
                {
                    "question": text[:300],
                    "header": str(q.get("header") or "").strip()[:12],
                    "options": options,
                    "multi_select": bool(q.get("multi_select")),
                }
            )
            if len(cleaned) >= 4:
                break
        if not cleaned:
            ask_result[tc.get("id")] = {
                "summary": "提问格式不合法，未发起提问",
                "error": (
                    "问题不能为空，且每题需要 2~4 个非空 label 的选项。"
                    "请修正后重新调用 ask_user_question。"
                ),
                "_duration_ms": None,
            }
            continue
        used = sum(len(r["questions"]) for r in question_requests)
        room = max(0, 4 - used)
        if not room:
            ask_result[tc.get("id")] = {
                "summary": "单轮最多向用户提 4 个问题",
                "error": "单轮最多 4 个问题，其余请合并或下一轮再问。",
                "_duration_ms": None,
            }
            continue
        question_requests.append(
            {"call_id": tc.get("id"), "questions": cleaned[:room]}
        )

    # ---- 决定收集：原生 interrupt 一次收齐（敏感操作 + 计划提交 + 提问）----
    approval: dict[str, dict] = {}
    plan_result: dict[str, dict] = {}  # exit_plan_mode 的合成结果（_execute 直接取用）
    plan_confirm_text: str | None = None
    if ops or plan_request or question_requests:
        payload: dict = {
            "kind": "approval",
            "conversation_id": rt.get("conv_id"),
            # 挂起批次标识：超时 watchdog 用它防止误伤后续轮次的审批
            "created_at": time.time(),
            "ops": ops,
        }
        if plan_request:
            payload["plan"] = plan_request
        if question_requests:
            payload["questions"] = question_requests
        resume_value = interrupt(payload)
        # 恢复段（本节点从头重跑后才会执行到这里）：副作用沿链路只发生一次
        decisions = (resume_value or {}).get("decisions") or {}
        for op in ops:
            d = decisions.get(op.get("call_id")) or {}
            approved = bool(d.get("approve"))
            reason = str(d.get("reason") or "").strip()
            command = str((op.get("arguments") or {}).get("command") or "")
            high_risk = op.get("risk") == "high"
            if approved:
                # 高危命令每次都必须重新确认：忽略客户端伪造的 remember 字段
                if not high_risk:
                    if d.get("remember_session") and command:
                        permission_manager.mark_session_allowed(
                            rt.get("conv_id"), command
                        )
                    if d.get("remember_forever") and command:
                        _remember_command_forever(db, command, settings)
                elif d.get("remember_session") or d.get("remember_forever"):
                    logger.info("忽略高危命令的记忆请求：%s", command[:120])
                entry = {
                    "ok": True,
                    "reason": "",
                    "remember": bool(
                        not high_risk
                        and (
                            d.get("remember_session") or d.get("remember_forever")
                        )
                    ),
                }
                bus.emit(
                    "permission_resolved",
                    {
                        "id": op.get("call_id"),
                        "approved": True,
                        "reason": "",
                        "risk": op.get("risk") or "",
                    },
                )
            else:
                entry = {
                    "ok": False,
                    "reason": reason or "用户拒绝了该操作（未收到有效决定）",
                }
                bus.emit(
                    "permission_resolved",
                    {
                        "id": op.get("call_id"),
                        "approved": False,
                        "reason": entry["reason"],
                        "risk": op.get("risk") or "",
                    },
                )
            for cid in [op.get("call_id"), *(op.get("also_covers") or [])]:
                if cid:
                    approval[cid] = entry
        # ---- 计划决定（恢复段应用，副作用只发生一次）----
        if plan_request:
            pd = (resume_value or {}).get("plan") or {}
            approved = bool(pd.get("approved"))
            steps = [
                str(s).strip()
                for s in (pd.get("steps") or plan_request["steps"])
                if str(s).strip()
            ][:8]
            if approved and steps:
                state["plan_steps"] = steps
                state["plan_map"] = [_plan_hint(s) for s in steps]
                state["plan_push_count"] = 0
                if db is not None and rt.get("conv_id") is not None:
                    try:
                        from ...todos import refresh_todos_for_plan

                        todos = refresh_todos_for_plan(db, rt["conv_id"], steps)
                        state["todos"] = todos or []
                        if state["todos"]:
                            bus.emit("todos", {"todos": state["todos"]})
                    except Exception as exc:
                        logger.warning("计划确认后播种任务清单失败：%s", exc)
                # 退出计划模式：写/命令恢复可执行（否则随后的敏感操作会被 plan_only 拦截）
                state["plan_only"] = False
                bus.emit("plan", {"steps": steps})
                plan_result[plan_request["call_id"]] = {
                    "summary": f"计划已确认（{len(steps)} 步），开始执行",
                    "_duration_ms": None,
                }
                plan_confirm_text = plan_confirmed_notice(steps)
            else:
                reason = str(pd.get("reason") or "").strip()
                plan_result[plan_request["call_id"]] = {
                    "summary": "用户未确认该计划",
                    "error": (
                        "用户未确认该计划"
                        + (f"：{reason}" if reason else "")
                        + "。请调整计划（可用 exit_plan_mode 重新提交）或先向用户说明。"
                    ),
                    "_duration_ms": None,
                }
        # ---- 用户回答（恢复段应用；缺失/跳过统一合成“用户跳过”）----
        # answers 经 /agent/resume 透传，按 call_id + index 对应；旧客户端
        # 不带 answers 时不会卡死——每题都按跳过回填，模型可继续或再问。
        answers_map = (resume_value or {}).get("answers") or {}
        for req in question_requests:
            given = (answers_map.get(req.get("call_id")) or {}).get("items") or []
            by_index: dict[int, dict] = {}
            for item in given:
                if not isinstance(item, dict):
                    continue
                try:
                    by_index[int(item.get("index"))] = item
                except (TypeError, ValueError):
                    continue
            rows: list[dict] = []
            answered = 0
            for idx, q in enumerate(req["questions"]):
                item = by_index.get(idx) or {}
                selected = [
                    str(s).strip()
                    for s in (item.get("selected") or [])
                    if str(s).strip()
                ]
                other = str(item.get("other") or "").strip()
                if item.get("skipped") or (not selected and not other):
                    answer = "（用户跳过了该问题）"
                else:
                    answered += 1
                    answer = "、".join(selected)
                    if other:
                        answer = f"{answer}；补充：{other}" if answer else other
                rows.append({"question": q["question"], "answer": answer})
            ask_result[req["call_id"]] = {
                "summary": f"用户已回答 {answered}/{len(rows)} 个问题",
                "answers": rows,
                "_duration_ms": None,
            }

    # 并行执行多个工具调用（检索类内部已有 GPU 锁；API 类并发安全），保持返回顺序
    tool_cache: dict = rt.setdefault("tool_cache", {})

    def _execute(tc: dict) -> tuple[dict, dict]:
        name = tc.get("name", "")
        tool = tools_by_name.get(name)
        args = tc.get("args") or {}
        if tool is None:
            return tc, {"error": f"未知工具：{name}", "_duration_ms": None}
        blocked_reason = blocked.get(tc.get("id"))
        if blocked_reason:
            return tc, {
                "summary": "计划模式：已拦截该敏感操作",
                "error": blocked_reason,
                "permission": "plan_only",
                "_duration_ms": None,
            }
        preset = plan_result.get(tc.get("id")) or ask_result.get(tc.get("id"))
        if preset is not None:
            # 计划提交/向用户提问不真正执行工具：结果由恢复段合成
            return tc, dict(preset)
        gate = approval.get(tc.get("id"))
        if gate is not None and not gate["ok"]:
            return tc, {
                "summary": f"用户拒绝了该操作：{gate['reason']}",
                "error": f"操作未执行（用户拒绝）：{gate['reason']}。请说明影响并询问替代方案。",
                "permission": "denied",
                "_duration_ms": None,
            }
        # ---- per-run 工具结果缓存：纯查询工具同参重复调用直接命中 ----
        # 键 = 工具名 + 规范化参数。只缓存查询类（见 _CACHEABLE_TOOLS），
        # 副作用类不缓存；error 结果不缓存（失败要重试）；命中时 summary
        # 标注"（缓存命中）"，前端/模型可见，且引用编号（index）保持不变，
        # 回答里的 [n] 依然指向同一来源。
        cache_key = None
        if name in _CACHEABLE_TOOLS:
            cache_key = _tool_cache_key(name, args)
            if cache_key is not None and cache_key in tool_cache:
                hit = dict(tool_cache[cache_key])
                if hit.get("error") is None:
                    if hit.get("summary"):
                        hit["summary"] = f"（缓存命中）{hit['summary']}"
                    return tc, hit
        # PreToolUse hooks：返回 deny 时拦截工具执行
        if db is not None:
            pre = run_hooks(db, "pre_tool_use", name, args)
            for r in pre:
                bus.emit(
                    "hook",
                    {
                        "event": "pre_tool_use",
                        "name": r["name"],
                        "tool": name,
                        "decision": r["decision"],
                        "reason": r["reason"],
                    },
                )
            denied = [r for r in pre if r.get("decision") == "deny"]
            if denied:
                reason = "；".join(r.get("reason") or "被 hook 拒绝" for r in denied)
                return tc, {
                    "summary": f"工具被 PreToolUse hook 拦截：{reason}",
                    "permission": "hook_denied",
                    "error": f"操作被 hook 拒绝：{reason}。请说明影响并询问替代方案。",
                    "_duration_ms": None,
                }
        if not args and _tool_has_required_args(tool):
            # 空参数兜底：schema 有必填字段且调用为空时不浪费一次工具调用，
            # 直接提示模型补充参数；全可选工具（如 MCP 工具）放行到 invoke，
            # 由 _mcp_default_args 补默认值
            return tc, {
                "summary": f"{name} 参数缺失，请补充参数后重试",
                "error": f"参数缺失：调用 {name} 需要必要参数，请补充后重试",
                "retry": True,
                "_duration_ms": None,
            }
        # 变更前预校验：目标缺失、枚举非法、新旧目标相同、转人工无用户授权。
        # 纯计算，无副作用；失败复用上面的 retry 协议。
        if effective_or(settings, "agent_tool_preflight", True):
            contract = (getattr(deps, "tool_contracts", None) or {}).get(name)
            reason = validate_call(
                contract,
                name,
                args,
                last_user_text=state.get("question") or "",
                check_consent=bool(
                    effective_or(settings, "agent_transfer_consent", True)
                ),
            )
            if reason:
                return tc, {
                    "summary": reason,
                    "error": reason,
                    "retry": True,
                    "_duration_ms": None,
                }
        try:
            t0 = time.perf_counter()
            if name.startswith("mcp_"):
                # LLM 显式传 JSON null 时，pydantic 模型（非 Optional 注解）
                # 会直接拒绝（"Input should be a valid ..."），发送前剔除；
                # 服务端对缺省与 null 语义无差别，且 zod .optional() 拒 null
                args = {k: v for k, v in (args or {}).items() if v is not None}
            result = tool.invoke(args)
            duration = round((time.perf_counter() - t0) * 1000)
            if not isinstance(result, dict):
                result = {"result": result}
            result["_duration_ms"] = duration
            # 成功结果写入 per-run 缓存（查询类工具）；
            # add_document 写库成功后清空检索类缓存——知识库内容已变，
            # 旧检索结果失效，不能继续命中
            if cache_key is not None and not result.get("error"):
                tool_cache[cache_key] = dict(result)
            # 写/改/删/命令成功后，同轮再读必须看到新状态：任何敏感操作都可能
            # 改动磁盘或知识库，清空 per-run 查询缓存（add_document 建索引同理）。
            if not result.get("error") and (
                name == "add_document" or is_sensitive_tool(name, args)
            ):
                tool_cache.clear()
            if gate is not None and gate.get("remember"):
                result["summary"] = (
                    str(result.get("summary") or "") + "（已记住，下次不再询问）"
                )
            return tc, result
        except Exception as exc:
            logger.warning("工具 %s 执行失败：%s", name, exc)
            return tc, {"error": str(exc), "_duration_ms": -1}

    outcomes: list[tuple[dict, dict]] = []
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(3, max(1, len(tool_calls)))) as pool:
        outcomes = list(pool.map(_execute, tool_calls))

    failed_ids: set[str] = set()
    hook_contexts: list[str] = []
    plan_entered = False
    for tc, result in outcomes:
        if stop_event is not None and stop_event.is_set():
            rt["status"] = "stopped"
            break
        name = tc.get("name", "")
        # ---- 计划模式控制工具：在这里改状态/发事件（工具本身无副作用）----
        # 注意：提示语必须在**所有 ToolMessage 追加完之后**再插进链里——
        # Anthropic 要求 tool_use 紧邻其 tool_result，中间夹 SystemMessage
        # 会直接 400（实测 messages.2 tool_use ids without tool_result）。
        if name == PLAN_ENTER_TOOL_NAME and "error" not in result:
            if not state.get("plan_only"):
                state["plan_only"] = True
                plan_entered = True
                bus.emit(
                    "status",
                    {"phase": "plan", "text": "已进入计划模式（只读探索）"},
                )
        entry = {
            "name": name,
            "arguments": tc.get("args") or {},
            "summary": "",
            "step": len(state["tool_trace"]) + 1,
            "duration_ms": result.pop("_duration_ms", None),
        }
        state["tool_trace"].append(entry)
        # 写后回读：在所有并行写完成后顺序执行（pool.map 之后），反映最终状态。
        # 回读不产生模型可见的 tool_call，也不写 tool_trace；只把压缩后的
        # 真实状态塞进本次结果，并记入 per-run ledger 供 finalize 核对。
        contract = (getattr(deps, "tool_contracts", None) or {}).get(name)
        if contract is not None and contract.mutating and isinstance(result, dict):
            try:
                ledger_entry = augment_mutation(
                    name=name,
                    args=tc.get("args") or {},
                    result=result,
                    tools_by_name=tools_by_name,
                    contract=contract,
                    is_sensitive=is_sensitive_tool,
                    enabled=bool(
                        effective_or(settings, "agent_tool_readback", True)
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - 回读失败不能影响工具结果
                logger.warning("写后回读失败（%s）：%s", name, exc)
                ledger_entry = None
            if ledger_entry:
                ledger_entry["call_id"] = tc.get("id") or ""
                if ledger_entry.get("verified") is not None:
                    result["_verified"] = ledger_entry["verified"]
                rt.setdefault("mutation_ledger", []).append(ledger_entry)
        bus.emit(
            "tool_start",
            {
                "id": tc.get("id"),
                "name": name,
                "arguments": tc.get("args") or {},
            },
        )

        # 工具结果「入链即定型」：ToolMessage 首次进入历史时就已瘦身，
        # 紧接着的 agent 节点看到的也是瘦身版（这是前缀缓存纯追加的前提）。
        # 阈值按工具类型分级：技能全文不截断；read_file / knowledge_base_search
        # 保留 6000；其余 2500。
        slim_result = _slim_tool_result(
            result, content_limit=tool_content_limit(name)
        )
        messages.append(
            ToolMessage(
                content=json.dumps(slim_result, ensure_ascii=False),
                name=name,
                tool_call_id=tc.get("id") or "",
            )
        )
        parsed = result if isinstance(result, dict) else {"summary": str(result)}
        entry["summary"] = parsed.get("summary", "")
        # 完整输出（截断版）随轨迹持久化：CLI /output 与 Web 展开复看
        entry["detail"] = _tool_detail(parsed)
        # 清洗工具结果摘要中的 XML 工具调用标记（deepseek 等模型的兜底输出）
        if entry["summary"]:
            import re as _re_summary
            entry["summary"] = _re_summary.sub(
                r"</?tool_calls[^>]*>|</?invoke[^>]*>|</?parameter[^>]*>|</?tool_use[^>]*>|</?function[^>]*>|</?arguments[^>]*>",
                "", entry["summary"],
            )
            entry["summary"] = _re_summary.sub(r"<[|]", "<", entry["summary"])
            entry["summary"] = entry["summary"].strip()[:400]
        if parsed.get("permission"):
            entry["permission"] = parsed.pop("permission")
        # 失败判定：显式 error 或命令非零退出码（exit_code 非 None 且非 0）
        if parsed.get("error") or parsed.get("exit_code") not in (None, 0):
            failed_ids.add(tc.get("id"))
        tool_sources = (
            extract_sources(parsed)
            if name in ("knowledge_base_search", "web_search")
            else []
        )
        state["sources"].extend(tool_sources)
        bus.emit(
            "tool_result",
            {
                "id": tc.get("id"),
                "name": name,
                "summary": parsed.get("summary", ""),
                "duration_ms": entry.get("duration_ms"),
                "sources": tool_sources,
                "detail": entry.get("detail", ""),
            },
        )
        if db is not None:
            for r in run_hooks(
                db,
                "post_tool_use",
                name,
                {"tool_input": tc.get("args") or {}, "tool_result": parsed},
            ):
                bus.emit(
                    "hook",
                    {
                        "event": "post_tool_use",
                        "name": r["name"],
                        "tool": name,
                        "decision": r["decision"],
                        "reason": r["reason"],
                    },
                )
                if r.get("additional_context"):
                    hook_contexts.append(f"[{r['name']}] {r['additional_context']}")

    if plan_entered:
        # 所有 ToolMessage 都已入链，此时插计划模式规则才不破坏 tool_use/tool_result 配对
        messages.append(SystemMessage(content=PLAN_MODE_SYSTEM_HINT))

    if plan_confirm_text:
        # 同上：必须在所有 ToolMessage 之后再插（Anthropic 的 tool_use/tool_result 相邻要求）
        messages.append(SystemMessage(content=plan_confirm_text))

    if hook_contexts:
        messages.append(SystemMessage(content=hook_context_notice(hook_contexts)))

    # 失败的工具调用不消耗迭代预算（成功数才累计）：
    # 路径/权限/参数类错误多为可重试问题，烧掉预算会导致任务半途而废（如 run#55）
    ok_count = len(tool_calls) - len(failed_ids)
    state["tool_calls_used"] += ok_count
    if failed_ids:
        state["failure_count"] = state.get("failure_count", 0) + len(failed_ids)
        for tc in tool_calls:
            if tc.get("id") not in failed_ids:
                continue
            name = tc.get("name", "")
            result = dict(next(r for t, r in outcomes if t.get("id") == tc.get("id")))
            error = str(result.get("error") or "")[:240]
            if result.get("output"):
                error = (error + "\n" + str(result.get("output"))[:240])[:420]
            messages.append(
                SystemMessage(content=tool_failure_notice(name, error))
            )

    # ---- 写后自动验证（verify）：按类型自动选命令或显式配置，失败可修复重试 ----
    written_paths = [
        str((tc.get("args") or {}).get("path") or "").strip()
        for tc in tool_calls
        if tc.get("name") in ("write_file", "edit_file")
        and tc.get("id") not in failed_ids
        and str((tc.get("args") or {}).get("path") or "").strip()
    ]
    if written_paths:
        verify_results = _run_verify(
            settings, written_paths, state.get("project_dir")
        )
        if verify_results:
            env_failures = [
                r for r in verify_results if r.get("environment_error")
            ]
            real_results = [
                r for r in verify_results if not r.get("environment_error")
            ]
            if env_failures and not verify_env_warned:
                # 解释器/路径不可用是环境问题：只提示一次，不按文件错误计数，
                # 也不诱导模型反复重写同一个文件。
                verify_env_warned = True
                _bump_patch(rt, "verify_env_missing")
                reason = (
                    env_failures[0].get("error")
                    or env_failures[0].get("output")
                    or ""
                )
                messages.append(
                    SystemMessage(content=verify_env_notice(str(reason)))
                )
                bus.emit(
                    "status",
                    {
                        "phase": "verify_env",
                        "text": "自动验证不可用，已跳过（不影响文件写入）",
                    },
                )
            failed = [
                r
                for r in real_results
                if r.get("exit_code") not in (None, 0) or r.get("error")
            ]
            lines = []
            for r in real_results:
                head = str(r.get("output") or "").strip() or r.get("error") or "（无输出）"
                lines.append(f"### {r['path']}  ({r['command']})\n{head[:300]}")
            if real_results and not failed:
                messages.append(SystemMessage(content=verify_pass_notice(lines)))
            elif failed:
                verify_fail_count = int(state.get("verify_fail_count") or 0) + 1
                state["verify_fail_count"] = verify_fail_count
                _bump_patch(rt, "verify_fail")
                max_retries = max(
                    0, int(effective(settings, "verify_max_retries") or 1)
                )
                retries_left = max(
                    0,
                    max_retries + 1 - verify_fail_count,
                )
                messages.append(
                    SystemMessage(
                        content=verify_fail_notice(
                            lines, len(failed), verify_fail_count, retries_left
                        )
                    )
                )
                bus.emit(
                    "status",
                    {
                        "phase": "verify",
                        "text": f"验证失败（{len(failed)} 个文件，第 {verify_fail_count} 次）",
                    },
                )

    # ---- 计划进度同步（进度由模型显式勾选，不再按调用数推断）----
    plan_steps = state.get("plan_steps") or []
    plan_map = state.get("plan_map") or [_plan_hint(s) for s in plan_steps]
    todos = state.get("todos") or []
    conv_id = rt.get("conv_id")
    # 工具执行后重新读取 DB，保证 todo_update 的修改立即反映到进度与前端。
    # 进度只来自清单本身：旧实现"1 次成功工具调用 = 推进 1 个计划步骤"会把
    # 一轮里为同一步做的多次检索算成多步（清单勾过头 → 硬约束提前放行），
    # 现在由模型用 todo_update complete 显式标记（系统提示词要求"每完成一步
    # 立即标记"），merge 与 finalize 另有两条按文本补勾的可靠路径。
    if db is not None and conv_id is not None:
        try:
            from ...todos import load_todos

            todos = load_todos(db, conv_id) or todos
        except Exception as exc:
            logger.warning("任务清单重新读取失败：%s", exc)
    done = 0
    total = 0
    remaining: list[dict] = []
    if todos:
        try:
            from ...todos import plan_progress

            done, total, remaining = plan_progress(todos)
        except Exception as exc:
            logger.warning("任务清单进度计算失败：%s", exc)
            done = total = 0
            remaining = []
    if not todos and plan_map:
        # 任务清单不可用（如 DB 降级/计划模式尚未确认）时退回计划步骤顺序。
        # 注意 plan_map 的字段是 "step" 而非 "text"，渲染时两者都要认，
        # 否则进度行会出现空步骤（"剩余步骤：；；；"）。
        done = min(int(state.get("plan_done_count") or 0), len(plan_map))
        total = len(plan_map)
        remaining = [
            {**m, "text": str(m.get("step") or m.get("text") or "")}
            for m in plan_map[done:]
        ]
    state["plan_done_count"] = done
    state["todos"] = todos
    if todos:
        bus.emit("todos", {"todos": todos})

    if total:
        progress_text = plan_progress_notice(done, total, remaining)
        # 进度行只在内容变化时追加：tools 节点每轮 ReAct 循环都会走到这里，
        # 无变化时重复追加会在链上堆叠同值行（实测单 run 达 13 行），每行都是
        # 缓存 miss 且此后常驻历史。前端 progress 事件仍照发，不影响 UI。
        if state.get("plan_progress_sig") != progress_text:
            state["plan_progress_sig"] = progress_text
            messages.append(SystemMessage(content=progress_text))
        # 计划进度事件：前端按完成/当前/未开始渲染步骤状态
        bus.emit(
            "plan_progress",
            {
                "done": done,
                "total": total,
                "current": remaining[0].get("text") if remaining else None,
                "text": progress_text,
            },
        )

    # ---- 任务态升格：本轮已产生副作用即按项目级任务给预算 ----
    # 关键词表永远补不全：实测"设计一个HTML页面，内容是svg绘制一个鹈鹕骑自行车
    # 的2D动画"（44 字 < 80、计划 3 步 < 4、零关键词命中）被判为非任务态，预算
    # 只有 12 次，两轮都撞上限被迫收尾。一旦真的动手写文件/跑命令，就该按
    # 项目级任务（30 次）给预算，而不是靠猜问题措辞。
    if not state.get("task_mode") and any(
        isinstance(tc, dict) and tc.get("name") in SENSITIVE_TOOLS
        for tc in tool_calls
    ):
        state["task_mode"] = True

    # 达到工具调用上限：强制收尾（下一轮 agent 不绑定工具）
    if _should_force_final(
        current=state["tool_calls_used"],
        threshold=_iteration_limit(state, settings),
        forced_final=bool(state["forced_final"]),
        status=rt.get("status"),
    ):
        state["forced_final"] = True
        if state.get("plan_only"):
            # 计划模式且尚未提交：不劝"汇报进度"（2026-10-10 实测该提示与提交
            # 要求打架，模型转而继续探索/正文汇报），只留"立即提交"一条路
            _bump_patch(rt, "plan_submit_forced")
            messages.append(SystemMessage(content=PLAN_SUBMIT_HINT_FORCED))
        else:
            _bump_patch(rt, "force_final_iterations")
            messages.append(
                SystemMessage(
                    content=budget_exhausted_notice(bool(state.get("task_mode")))
                )
            )

    # 失败次数过多仍受阻：强制收尾并让模型向用户说明（避免无意义空转）
    max_failures = _failure_limit(state, settings)
    if _should_force_final(
        current=state.get("failure_count", 0),
        threshold=max_failures,
        forced_final=bool(state["forced_final"]),
        status=rt.get("status"),
    ):
        state["forced_final"] = True
        _bump_patch(rt, "force_final_failures")
        messages.append(
            SystemMessage(
                content=failure_limit_notice(bool(state.get("task_mode")))
            )
        )

    return {
        "messages": messages,
        "pending_tool_calls": [],
        "tool_trace": state["tool_trace"],
        "sources": state["sources"],
        "tool_calls_used": state["tool_calls_used"],
        "failure_count": state.get("failure_count", 0),
        "forced_final": bool(state["forced_final"]),
        "last_call_warned": state["last_call_warned"],
        "verify_fail_count": int(state.get("verify_fail_count") or 0),
        "verify_env_warned": verify_env_warned,
        "plan_done_count": state.get("plan_done_count", 0),
        "plan_progress_sig": state.get("plan_progress_sig", ""),
        "task_mode": bool(state.get("task_mode")),
        "force_continue": False,
        "plan_push_count": state.get("plan_push_count", 0),
        # 计划模式：进入后 plan_only 生效，敏感工具被拦截（见本函数开头）
        "plan_only": bool(state.get("plan_only")),
        "plan_steps": state.get("plan_steps") or [],
        "plan_map": state.get("plan_map") or [],
        "todos": state.get("todos") or [],
    }


# ============================================================
# 节点：finalize（收尾）
# ============================================================
