"""tools 节点：工具执行（并行/HITL/失败重试）、计划进度同步、checkpoint 保存（自 langgraph_agent.py 拆分）。"""

from __future__ import annotations

import json
import logging
import threading
import time

from sqlalchemy.orm import Session

from ...config import Settings
from ...runtime_config import effective

logger = logging.getLogger(__name__)
from ..state import AgentState, EventBus
from .common import (_failure_limit, _generate_reasoning_summary,
                     _iteration_limit, _plan_hint)
from ..utils import (
    _CACHEABLE_TOOLS,
    _run_verify,
    _slim_tool_result,
    _tool_cache_key,
    _tool_detail,
    _tool_has_required_args,
)

from langchain_core.messages import SystemMessage, ToolMessage
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..langgraph_agent import LangGraphAgentService  # 仅类型注解
from ..tools import extract_sources

# ==== 函数体（原文）====
def _tools_node(state: AgentState) -> dict:
    service: LangGraphAgentService = state["service"]
    bus: EventBus = state["bus"]
    settings: Settings = service.settings
    runtime: dict = state["runtime"]
    messages = state["messages"]
    db: Session | None = state["db"]
    stop_event = state.get("stop_event")
    from ...permissions import (
        describe_tool_call,
        display_args,
        get_permission_manager,
        is_sensitive_tool,
    )
    from ...tools_extra import command_allowed
    from ...hooks import run_hooks

    permission_manager = get_permission_manager()
    try:
        permission_manager.cleanup()
    except Exception:
        pass

    tools_by_name = {t.name: t for t in state["tools"]}
    counter = state["counter"]
    tool_calls = state.get("pending_tool_calls") or []
    if not tool_calls:
        return {}

    # 预生成"思考摘要"（后台线程）：与工具执行并行，
    # 工具通常耗时数秒，摘要利用这段时间完成，作答轮几乎零额外延迟
    if settings.reasoning_summary_enabled:
        try:
            evt = {"summary": None, "done": threading.Event()}
            runtime["_reasoning"] = evt

            def _pre_generate():
                try:
                    text = _generate_reasoning_summary(service, state, runtime)
                    evt["summary"] = (text or "")[:200]
                except Exception:
                    evt["summary"] = ""
                finally:
                    evt["done"].set()

            threading.Thread(target=_pre_generate, daemon=True).start()
        except Exception as exc:
            logger.warning("启动思考摘要预生成失败：%s", exc)

    # ---- 计划模式（只读）：敏感操作直接拦截，不弹审批 ----
    blocked: dict[str, str] = {}
    if state.get("plan_only"):
        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("args") or {}
            if is_sensitive_tool(name, args):
                blocked[tc.get("id")] = (
                    "当前为计划模式（只读）：不允许写文件/编辑/删除/执行命令。"
                    "请基于已有信息整理出清晰的执行计划，等待用户确认后再执行。"
                )

    # ---- 人工确认（HITL）：敏感操作先请求用户批准，再进入执行 ----
    # 写/编辑/删除/命令默认 ask；命令命中自动放行白名单或本会话已记住则无需确认；
    # permission_mode=allow 时全部自动批准（类 Claude Code --dangerously-skip-permissions）。
    approval: dict[str, dict] = {}
    permission_mode = effective(settings, "tool_permission_mode", "ask")
    if permission_mode == "ask":
        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("args") or {}
            if tc.get("id") in blocked:
                continue
            needs = is_sensitive_tool(name, args)
            command = str(args.get("command") or "")
            if name in ("bash", "command_tool") and (
                command_allowed(settings, command)[0]
                or permission_manager.is_session_allowed(runtime.get("conv_id"), command)
            ):
                needs = False
            if not needs:
                continue
            summary = describe_tool_call(name, args)
            req = permission_manager.submit(
                tool=name,
                arguments=display_args(name, args),
                summary=summary,
                conversation_id=runtime.get("conv_id"),
            )
            bus.emit(
                "permission_request",
                {
                    "id": req.id,
                    "name": name,
                    "arguments": req.arguments,
                    "summary": summary,
                },
            )
            bus.emit("status", {"phase": "permission", "text": f"等待确认：{summary}"})
            approved = permission_manager.wait(
                req,
                timeout=int(effective(settings, "permission_timeout", 0) or 0),
                stop_event=stop_event,
            )
            if approved:
                bus.emit("permission_resolved", {"id": req.id, "approved": True, "reason": ""})
                if req.remember_session and command:
                    permission_manager.mark_session_allowed(
                        runtime.get("conv_id"), command
                    )
                approval[tc.get("id")] = {
                    "ok": True,
                    "reason": "",
                    "remember": bool(req.remember_session or req.remember_forever),
                }
            else:
                reason = req.reason or (
                    "用户拒绝了该操作" if req.status == "denied" else "等待确认超时，已自动取消"
                )
                bus.emit("permission_resolved", {"id": req.id, "approved": False, "reason": reason})
                approval[tc.get("id")] = {"ok": False, "reason": reason}

    # 并行执行多个工具调用（检索类内部已有 GPU 锁；API 类并发安全），保持返回顺序
    tool_cache: dict = runtime.setdefault("tool_cache", {})

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
            if name == "add_document" and not result.get("error"):
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
    for tc, result in outcomes:
        if stop_event is not None and stop_event.is_set():
            runtime["status"] = "stopped"
            break
        name = tc.get("name", "")
        entry = {
            "name": name,
            "arguments": tc.get("args") or {},
            "summary": "",
            "step": len(state["tool_trace"]) + 1,
            "duration_ms": result.pop("_duration_ms", None),
        }
        state["tool_trace"].append(entry)
        bus.emit(
            "tool_start",
            {
                "id": tc.get("id"),
                "name": name,
                "arguments": tc.get("args") or {},
            },
        )

        # 工具结果进历史前瘦身：content 类字段超过阈值时保留首尾、
        # 中间省略（Hermes proactive_prune 思路）。模型已在本轮看过完整
        # 输出，下一轮只需 summary + 关键片段；小输出原样保留，
        # 不破坏工具已发的缓存前缀。
        slim_result = _slim_tool_result(result)
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

    if hook_contexts:
        messages.append(
            SystemMessage(
                content="PostToolUse hook 附加信息：\n" + "\n".join(hook_contexts)
            )
        )

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
                SystemMessage(
                    content=(
                        f"工具 {name} 执行失败：{error}\n"
                        "这是一次可重试的失败，不消耗你的工具调用预算。"
                        "请换一种方式继续完成任务，不要就此停止：\n"
                        "1. 先 list_dir / read_file / grep_search 确认路径与现状；\n"
                        "2. 按错误信息修正参数（例如改用工作目录内的相对路径，或先创建父目录）；\n"
                        "3. 若目标确实不可达（如在工作目录之外），向用户说明限制，"
                        "并给出可落地的替代方案（写入工作目录内、修改工作目录等），"
                        "询问用户后再继续。\n"
                        "任务未完成前，请持续尝试合理的替代方法。"
                    )
                )
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
        verify_results = _run_verify(settings, written_paths)
        if verify_results:
            failed = [
                r
                for r in verify_results
                if r.get("exit_code") not in (None, 0) or r.get("error")
            ]
            lines = []
            for r in verify_results:
                head = str(r.get("output") or "").strip() or r.get("error") or "（无输出）"
                lines.append(f"### {r['path']}  ({r['command']})\n{head[:300]}")
            if not failed:
                messages.append(
                    SystemMessage(
                        content=(
                            "写后自动验证结果（全部通过）：\n"
                            + "\n\n".join(lines)
                            + "\n\n可以继续后续步骤。"
                        )
                    )
                )
            else:
                verify_fail_count = int(state.get("verify_fail_count") or 0) + 1
                state["verify_fail_count"] = verify_fail_count
                max_retries = max(
                    0, int(effective(settings, "verify_max_retries") or 1)
                )
                retries_left = max(
                    0,
                    max_retries + 1 - verify_fail_count,
                )
                msg = (
                    f"写后自动验证失败（{len(failed)} 个文件，第 {verify_fail_count} 次失败）：\n"
                    + "\n\n".join(lines)
                )
                if retries_left > 0:
                    msg += (
                        f"\n\n请根据上面的错误修复文件（edit_file/write_file）并等待复验，"
                        f"还有 {retries_left} 次复验机会。"
                    )
                else:
                    msg += (
                        "\n\n已到验证重试上限。请停止继续修复，"
                        "向用户如实说明验证未通过的原因、影响范围与建议。"
                    )
                messages.append(SystemMessage(content=msg))
                bus.emit(
                    "status",
                    {
                        "phase": "verify",
                        "text": f"验证失败（{len(failed)} 个文件，第 {verify_fail_count} 次）",
                    },
                )

    # ---- 计划进度同步 + 任务清单自动更新（硬约束）----
    plan_steps = state.get("plan_steps") or []
    plan_map = state.get("plan_map") or [_plan_hint(s) for s in plan_steps]
    todos = state.get("todos") or []
    conv_id = runtime.get("conv_id")
    # 工具执行后重新读取 DB，保证 todo_update 的修改立即反映到进度与前端
    if db is not None and conv_id is not None:
        try:
            from ...todos import load_todos

            todos = load_todos(db, conv_id) or todos
        except Exception as exc:
            logger.warning("任务清单重新读取失败：%s", exc)
    # 成功的非 todo_update 工具调用推进一个计划步骤；
    # todo_update 只维护清单（list/add/complete/remove/set），不推进计划进度
    advance = sum(
        1
        for tc in tool_calls
        if tc.get("id") not in failed_ids and tc.get("name") != "todo_update"
    )
    if plan_steps and advance > 0:
        prev_done = int(state.get("plan_done_count") or 0)
        target = min(prev_done + advance, len(plan_steps))
        if db is not None and conv_id is not None:
            try:
                from ...todos import sync_todos_from_plan

                hints = [_plan_hint(s).get("tool_hint") for s in plan_steps]
                todos = (
                    sync_todos_from_plan(db, conv_id, plan_steps, target, hints=hints)
                    or todos
                )
            except Exception as exc:
                logger.warning("任务清单自动同步失败：%s", exc)
        else:
            # DB 不可用时仅推进内存进度
            state["plan_done_count"] = target
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
        # 任务清单不可用（如 DB 降级）时退回计划步骤顺序
        done = min(int(state.get("plan_done_count") or 0), len(plan_map))
        total = len(plan_map)
        remaining = plan_map[done:]
    state["plan_done_count"] = done
    state["todos"] = todos
    if todos:
        bus.emit("todos", {"todos": todos})

    if total:
        progress = [f"计划进度：已完成 {done}/{total} 步。"]
        if remaining:
            current = remaining[0]
            progress.append(f"当前步骤：{current.get('text', '')}")
            progress.append(
                "剩余步骤："
                + "；".join(str(t.get("text", "")) for t in remaining)
            )
            if any(
                _plan_hint(str(t.get("text", ""))).get("tool_hint")
                for t in remaining
            ):
                progress.append(
                    "硬约束：剩余步骤尚未完成，请继续按顺序执行；不要提前输出最终回答。"
                )
        else:
            progress.append("剩余：无，可以基于已获取的信息作答。")
        progress_text = "\n".join(progress)
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

    # 达到工具调用上限：强制收尾（下一轮 agent 不绑定工具）
    if (
        state["tool_calls_used"] >= _iteration_limit(state, settings)
        and not state["forced_final"]
        and runtime.get("status") != "stopped"
    ):
        state["forced_final"] = True
        messages.append(
            SystemMessage(
                content=(
                    (
                        "本轮项目任务的工具调用预算已用尽，但任务尚未完成。"
                        "请给出【进度汇报】：已完成了哪些步骤、还有哪些未完成、"
                        "下一步计划做什么；并明确告诉用户：回复“继续”即可接着做。"
                        "不要假装任务已完成。"
                    )
                    if state.get("task_mode")
                    else (
                        "你已达到本轮允许的工具调用次数上限。"
                        "请基于已经获取的信息直接给出最终回答；"
                        "如果信息仍然不足，请明确告诉用户缺少什么，不要继续调用工具。"
                    )
                )
            )
        )

    # 失败次数过多仍受阻：强制收尾并让模型向用户说明（避免无意义空转）
    max_failures = _failure_limit(state, settings)
    if (
        state.get("failure_count", 0) >= max_failures
        and not state["forced_final"]
        and runtime.get("status") != "stopped"
    ):
        state["forced_final"] = True
        messages.append(
            SystemMessage(
                content=(
                    (
                        "你已连续多次遇到工具执行失败（超过允许的失败重试上限）。"
                        "请给出【进度汇报】：任务卡在哪一步、失败原因、用户需要做什么"
                        "（例如修改工作目录、提供权限、确认替代路径）；"
                        "并告诉用户回复“继续”即可接着做。不要再尝试调用工具。"
                    )
                    if state.get("task_mode")
                    else (
                        "你已连续多次遇到工具执行失败（超过允许的失败重试上限）。"
                        "请基于已经获得的信息给出最终回答，向用户如实说明："
                        "任务卡在哪一步、失败原因、以及用户需要做什么（例如修改工作目录、"
                        "提供权限、确认替代路径）。不要再尝试调用工具。"
                    )
                )
            )
        )

    # checkpoint：每轮工具执行后保存快照，异常中断可恢复
    if settings.checkpoint_enabled and db is not None and runtime.get("conv_id"):
        try:
            from ...checkpoint import get_store

            get_store(settings).save(
                runtime["conv_id"],
                {
                    "messages": messages,
                    "sources": state["sources"],
                    "tool_trace": state["tool_trace"],
                    "plan_steps": state.get("plan_steps") or [],
                    "plan_map": state.get("plan_map") or [],
                    "final_text": runtime.get("final_text") or "",
                    "tool_calls_used": state["tool_calls_used"],
                    "failure_count": state.get("failure_count", 0),
                    "forced_final": state["forced_final"],
                    "last_call_warned": state["last_call_warned"],
                    "task_mode": bool(state.get("task_mode")),
                    "verify_fail_count": int(state.get("verify_fail_count") or 0),
                    "plan_done_count": state.get("plan_done_count", 0),
                    "plan_push_count": state.get("plan_push_count", 0),
                    "dispatch_done": bool(state.get("dispatch_done")),
                    "todos": state.get("todos") or [],
                },
            )
        except Exception as exc:
            logger.warning("保存 checkpoint 失败：%s", exc)

    return {
        "messages": messages,
        "pending_tool_calls": [],
        "tool_trace": state["tool_trace"],
        "sources": state["sources"],
        "tool_calls_used": state["tool_calls_used"],
        "failure_count": state.get("failure_count", 0),
        "forced_final": state["forced_final"],
        "last_call_warned": state["last_call_warned"],
        "verify_fail_count": int(state.get("verify_fail_count") or 0),
        "plan_done_count": state.get("plan_done_count", 0),
        "force_continue": False,
        "plan_push_count": state.get("plan_push_count", 0),
        "todos": state.get("todos") or [],
    }


# ============================================================
# 节点：finalize（收尾）
# ============================================================


