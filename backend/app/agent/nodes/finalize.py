"""finalize 节点：来源去重/联网附录/消息持久化/运行记录与 trace/标题后置（自 langgraph_agent.py 拆分）。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from sqlalchemy.orm import Session

from ...config import Settings
from ...db import repository as repo
from ...runtime_config import chat_provider_config, effective, effective_or

logger = logging.getLogger(__name__)
from ..state import AgentState, EventBus
from .common import _bump_patch, _plan_hint
from ..utils import _strip_xml_tool_tags

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..langgraph_agent import LangGraphAgentService  # 仅类型注解
from ...tracing import usage_summary, usage_summary_with_aux, write_trace

# 来源条数上限：只防病态超长列表。counter 是整轮对话的全局引用编号器
# （多轮 kb_search 持续递增），一个答案可能合法引用到 [100]+；因此上限放宽到
# 200，且**正文实际引用到的条目一律保留**（见 dedupe_sources / _fit_sources_bytes）。
MAX_SOURCES = 200
# messages.sources 是 MySQL TEXT（64KB），给 JSON 留余量；超出时优先缩正文、不丢被引用条目
SOURCES_MAX_BYTES = 58_000


def _dispatch_summary(state: AgentState, rt: dict | None) -> dict:
    """本轮派发汇总：轮数、子任务数、模式分布、错误与降级数。

    只做观测，不影响回答；结果从 reducer 保留的 subagent_results 派生，
    不新增 AgentState 字段（状态契约测试要求每个返回键都声明）。
    """
    results = [
        item
        for item in (state.get("subagent_results") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    modes: dict[str, int] = {}
    models: dict[str, int] = {}
    profiles: dict[str, int] = {}
    errors = 0
    cancelled = 0
    for item in results:
        mode = str(item.get("mode") or "research")
        modes[mode] = modes.get(mode, 0) + 1
        model = str(item.get("model") or "").strip()
        if model:
            models[model] = models.get(model, 0) + 1
        profile = str(item.get("profile") or "").strip()
        if profile:
            profiles[profile] = profiles.get(profile, 0) + 1
        if item.get("error"):
            errors += 1
        if item.get("cancelled"):
            cancelled += 1
    return {
        "rounds": int(state.get("dispatch_rounds") or 0),
        "subtasks": len(results),
        "modes": modes,
        "models": models,
        "profiles": profiles,
        "errors": errors,
        "cancelled": cancelled,
        "downgraded": int((rt or {}).get("dispatch_downgraded") or 0),
    }


def _fit_sources_bytes(items: list[dict], cited: set[int]) -> list[dict]:
    """把来源 JSON 压到 TEXT 列可存的大小：优先缩正文，绝不丢被引用条目。"""

    def size(seq: list[dict]) -> int:
        try:
            return len(json.dumps(seq, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            return 0

    if size(items) <= SOURCES_MAX_BYTES:
        return items
    trimmed = [dict(item) for item in items]
    for item in trimmed:
        content = item.get("content")
        if isinstance(content, str) and len(content) > 200:
            item["content"] = content[:200] + "…"
    if size(trimmed) <= SOURCES_MAX_BYTES:
        return trimmed
    for item in trimmed:
        content = item.get("content")
        if isinstance(content, str) and len(content) > 80:
            item["content"] = content[:80] + "…"
    if size(trimmed) <= SOURCES_MAX_BYTES:
        return trimmed
    # 仍超限：丢未引用条目，被引用条目永远保留
    keep = []
    for item in trimmed:
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            idx = None
        if idx is not None and idx in cited:
            keep.append(item)
    if keep and size(keep) <= SOURCES_MAX_BYTES:
        return keep
    # 极端情况：被引用条目本身超限，清空正文只留元数据（角标仍可定位）
    minimal = []
    for item in keep or trimmed:
        slim = dict(item)
        slim["content"] = ""
        minimal.append(slim)
    return minimal


def dedupe_sources(
    sources: list[dict],
    limit: int = MAX_SOURCES,
    cited: set[int] | None = None,
) -> list[dict]:
    """来源去重、按引用编号排序，并保证正文 [n] 引用的条目全部保留。

    并行/混合工具调用时来源按完成顺序入账（实测出现过 [6,7,8,9,10,1,2,3]），
    不排序会让编号看起来乱序；按 index 去重后，cited 集合中的条目一律保留，
    上限只约束未被引用的其余条目（TEXT 列体积由 _fit_sources_bytes 兜底）。
    """
    deduped: list[dict] = []
    seen: set[str] = set()
    for item in sources or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("index") or item.get("content", ""))
        if key and key not in seen:
            seen.add(key)
            deduped.append(item)

    def _order(item: dict) -> tuple[int, int]:
        try:
            return (0, int(item.get("index")))
        except (TypeError, ValueError):
            return (1, 0)

    def _idx(item: dict) -> int | None:
        try:
            return int(item.get("index"))
        except (TypeError, ValueError):
            return None

    deduped.sort(key=_order)
    cited_set = cited or set()
    if cited_set:
        cited_items = [item for item in deduped if _idx(item) in cited_set]
        rest = [item for item in deduped if _idx(item) not in cited_set]
        if len(cited_items) >= limit:
            out = cited_items
        else:
            out = cited_items + rest[: limit - len(cited_items)]
        out.sort(key=_order)
        return _fit_sources_bytes(out, cited_set)
    return _fit_sources_bytes(deduped[:limit], cited_set)


def _scope_meta(state: AgentState) -> dict:
    """本轮限定范围的溯源信息（回放面板展示；空范围不落库）。"""
    paths = [str(p) for p in (state.get("kb_scope_paths") or [])]
    if not paths:
        return {}
    return {
        "label": state.get("kb_scope_label") or "",
        "files": paths[:20],
        "count": len(paths),
    }


def _quote_meta(state: AgentState) -> dict:
    """划选原文的溯源信息（只存元数据，不重复存全文）。"""
    quote = state.get("quote") or {}
    text = str(quote.get("text") or "")
    if not text:
        return {}
    return {
        "source": str(quote.get("source") or ""),
        "page": quote.get("page"),
        "chars": len(text),
        "context_chars": len(str(quote.get("context") or "")),
        "sha1": hashlib.sha1(text.encode("utf-8")).hexdigest()[:12],
    }


def _compute_latency_ms(deps, rt: dict) -> int:
    """本段实际执行耗时 + 挂起前累计；不含人工审批等待。

    段起点只存在于请求级 Deps（不进检查点）：跨进程恢复时不会拿到另一个
    进程的 perf_counter 基准，因此不会产生负数或异常巨值。
    """
    base = int(getattr(deps, "elapsed_ms", 0) or (rt or {}).get("elapsed_ms") or 0)
    started = float(getattr(deps, "started", 0.0) or 0.0)
    segment = (
        max(0, round((time.perf_counter() - started) * 1000))
        if started
        else 0
    )
    return base + segment


# ==== 函数体（原文）====
def _finalize_node(state: AgentState, runtime) -> dict:
    """第二参数是 LangGraph 按名注入的 Runtime；图外兜底直调时传 `runtime_of(deps)`。"""
    deps = runtime.context
    service: LangGraphAgentService = deps.service
    bus: EventBus = deps.bus
    db: Session | None = deps.db
    settings: Settings = service.settings
    rt: dict = state["runtime"]

    # 收尾阶段状态：客户端据此显示「正在核对来源与引用… / 快好了…」
    if bus is not None:
        bus.emit("status", {"phase": "finalizing", "text": "正在整理回答与来源…"})

    final_text = rt.get("final_text") or ""
    # 收尾清理：完整文本上再清一次 XML 工具标记 + strip（分片清理可能漏跨片标签）
    cleaned_final = _strip_xml_tool_tags(final_text).strip()
    if cleaned_final != final_text:
        final_text = cleaned_final
        rt["final_text"] = cleaned_final
    sources = state.get("sources") or []
    tool_trace = state.get("tool_trace") or []
    conv_id = rt.get("conv_id")

    # 空正文兜底：输出预算被思考吃光或上游卡死时可能一个字都没有。
    # 静默返回空回答且 status=ok 会让用户完全不知道发生了什么
    # （2026-09-24 实测 answer_len=0 / status=ok / 前端"没有回答"），
    # 这里降级为明确错误并推送 error 事件。
    if not final_text and rt.get("status") == "ok":
        rt["status"] = "error"
        _bump_patch(rt, "empty_answer")
        rt["error"] = rt.get("error") or (
            "本轮未产出正文（输出预算可能被思考耗尽，或上游流中断）。请重试；"
            "若反复出现，检查供应商状态或调高 max_tokens。"
        )
        bus.emit(
            "error",
            {
                "message": rt["error"],
                "phase": "finalize",
                "code": "empty_answer",
            },
        )

    # 收尾同步任务清单：纯推理/总结类步骤视为被最终回答覆盖，自动补完成；
    # 工具型步骤若仍未完成则保留未勾选状态（审计留痕，不假装完成）
    if db is not None and conv_id is not None and rt.get("status") == "ok":
        try:
            from ...todos import complete_steps_by_text, load_todos, plan_progress

            plan_steps = state.get("plan_steps") or []
            final_steps = [
                s for s in plan_steps if not _plan_hint(s).get("tool_hint")
            ]
            todos = load_todos(db, conv_id)
            if final_steps:
                todos = complete_steps_by_text(db, conv_id, final_steps) or todos
            if todos:
                state["todos"] = todos
                done, total, remaining = plan_progress(todos)
                state["plan_done_count"] = done
                bus.emit("todos", {"todos": todos})
                if total:
                    # 收尾也要发最终进度，否则右上角计数停留在上一次 plan_progress
                    bus.emit(
                        "plan_progress",
                        {
                            "done": done,
                            "total": total,
                            "current": (
                                remaining[0].get("text") if remaining else None
                            ),
                            "text": f"计划进度：已完成 {done}/{total} 步。",
                        },
                    )
        except Exception as exc:
            logger.warning("收尾任务清单同步失败：%s", exc)

    # 来源去重 + 按编号排序；正文引用到的 [n] 一律保留（多轮检索编号可超过旧的 50 上限）
    cited_indices = {int(m) for m in re.findall(r"\[(\d{1,3})\]", final_text or "")}
    deduped = dedupe_sources(sources, cited=cited_indices)

    # 联网来源自动附录：模型未标注 [n] 时补"参考来源"链接
    appendix = ""
    if final_text and not re.search(r"\[\d{1,3}\]", final_text):
        web_sources = [s for s in deduped if s.get("type") == "web" and s.get("url")]
        if web_sources:
            refs = "\n".join(
                f"{s.get('index')}. [{s.get('title', '来源')}]({s.get('url')})"
                for s in web_sources
            )
            appendix = f"\n\n**参考来源**\n{refs}"
            final_text += appendix
            rt["final_text"] = final_text

    # 答复核对：mutation ledger 里的关键事实（ID/金额/状态）若未出现在最终答复，
    # 追加一段系统核对块。只补结构化事实，不改写模型原文；必须在持久化之前，
    # 让落库文本与用户看到的文本一致。
    lint_appended = ""
    if final_text and rt.get("mutation_ledger"):
        try:
            if effective_or(settings, "agent_answer_lint", True):
                from ..answer_lint import apply_lint

                final_text, lint_appended, missing = apply_lint(
                    final_text, rt.get("mutation_ledger") or []
                )
                rt["final_text"] = final_text
                if lint_appended:
                    _bump_patch(rt, "answer_lint")
                    rt["answer_lint_missing"] = len(missing)
        except Exception as exc:
            logger.warning("答复核对失败：%s", exc)

    # 持久化 user 消息 + assistant 消息
    if db is not None and conv_id is not None:
        try:
            # 工具轮消息持久化（缓存前缀保真 + 跨轮工具记忆）：
            # AIMessage(带 tool_calls) -> role='assistant'，content 为
            # {"__tool_calls__": [...]} 标记 JSON；
            # ToolMessage -> role='tool'，content 原样（slim 版），
            # tool_trace 存 [{"tool_call_id","name"}] 供重建配对。
            # 否则下一 run 从 DB 重建历史时缺少工具轮，前缀与上一 run
            # 末次调用不一致 → DeepSeek 缓存整体失效（实测编码任务
            # 命中率仅 21-59%，简单问答 92%）。
            # 纯追加链落库：从 persist_start（本轮 D 块起点）开始按链顺序
            # 持久化 system（D 块/轮内提示）、tool、工具轮 assistant 行，
            # 用户问题行插在链中的对应位置——下一轮从 DB 重建的历史与
            # 上一轮实际发送的消息逐字节一致（DeepSeek 缓存前缀保真）。
            chain = state.get("messages") or []
            persist_start = state.get("persist_start")
            if persist_start is None:
                # 兜底（异常路径）：最后一个 HumanMessage 之后
                persist_start = 0
                for idx in range(len(chain) - 1, -1, -1):
                    if isinstance(chain[idx], HumanMessage):
                        persist_start = idx + 1
                        break
            q_inserted = False
            for m in chain[persist_start:]:
                if isinstance(m, HumanMessage):
                    if not q_inserted:
                        repo.add_message(db, conv_id, "user", state["question"])
                        q_inserted = True
                    continue
                if isinstance(m, SystemMessage):
                    repo.add_message(db, conv_id, "system", m.content)
                elif isinstance(m, ToolMessage):
                    repo.add_message(
                        db,
                        conv_id,
                        "tool",
                        m.content if isinstance(m.content, str) else str(m.content),
                        tool_trace=[
                            {
                                "tool_call_id": m.tool_call_id or "",
                                "name": m.name or "",
                            }
                        ],
                    )
                elif isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                    marker = {"__tool_calls__": list(m.tool_calls)}
                    # DeepSeek 思考模式的 reasoning_content 需随消息原样传回，
                    # **空字符串也必须回传空串**（否则下次请求 400）——
                    # 无条件写入 marker（模型未输出时存空串）
                    reasoning = (getattr(m, "additional_kwargs", {}) or {}).get(
                        "reasoning_content"
                    )
                    marker["__reasoning__"] = (
                        reasoning if reasoning is not None else ""
                    )
                    # Anthropic 格式的思考块（含 signature）也要落库：下一轮从
                    # DB 重建历史时若不带上，工具轮同样会 400（回传要求跨轮成立）
                    thinking_blocks = (
                        getattr(m, "additional_kwargs", {}) or {}
                    ).get("anthropic_thinking")
                    if thinking_blocks:
                        marker["__thinking__"] = list(thinking_blocks)
                    repo.add_message(
                        db,
                        conv_id,
                        "assistant",
                        json.dumps(marker, ensure_ascii=False),
                    )
                # 无 tool_calls 的 AIMessage（最终回答）单独落库，这里跳过
            if not q_inserted:
                repo.add_message(db, conv_id, "user", state["question"])
            if rt.get("status") != "stopped" and final_text:
                repo.add_message(
                    db,
                    conv_id,
                    "assistant",
                    final_text,
                    tool_trace=tool_trace,
                    sources=deduped,
                )
        except Exception as exc:
            # 单条落库失败会让 Session 处于 failed 事务，后续写入全部连带失败
            # （2026-09-24 实测：tool_trace 溢出后 agent_runs 与轨迹压缩一起丢）
            db.rollback()
            logger.warning("保存对话消息失败：%s", exc)

    # 决策运行记录 + 结构化 trace（可观测性）
    if db is not None:
        try:
            run_status = (
                "stopped"
                if rt.get("status") == "stopped"
                else "error"
                if rt.get("error")
                else "ok"
            )
            # 记录本次运行实际使用的模型：用量面板按模型/峰谷价估算成本
            try:
                chat_model = (chat_provider_config(settings) or {}).get("model")
            except Exception:
                chat_model = None
            dispatch_meta = _dispatch_summary(state, rt)
            run = repo.create_agent_run(
                db,
                conversation_id=conv_id,
                question=state["question"],
                plan=state.get("plan_steps") or [],
                tool_trace=tool_trace,
                answer_len=len(final_text),
                latency_ms=_compute_latency_ms(deps, rt),
                status=run_status,
                error=rt.get("error"),
                token_usage={                    **usage_summary_with_aux(),
                    "model": chat_model,
                    "last_call": rt.get("token_usage"),
                    # 逐调用遥测：input/output/cache_read，定位轮内断链
                    "calls": rt.get("calls") or [],
                    # 分阶段耗时：prepare/dispatch/subagent/merge/agent/tools/finalize + TTFT
                    "timings": dict(rt.get("timings") or {}),
                    # 工具定义哈希（缓存前缀监测）：与上一 run 对比，
                    # 变化说明前缀被破坏，命中率可能下降
                    "tools_hash": rt.get("tools_hash"),
                    # 工具面体积（字符数）：每次调用都在输入里（缓存命中时也计费），
                    # 用于验收"工具瘦身"并防止无意识膨胀
                    "tools_chars": rt.get("tools_chars"),
                    # 工具面分项（core vs MCP）：定位体积来源，防止 MCP 静默膨胀
                    "tools_breakdown": rt.get("tools_breakdown"),
                    # 技能索引统计（可见数/names-only/字符数/签名）：验证不再静默截断
                    "skills_index": rt.get("skills_index"),
                    # 本轮实际使用的提示词模板/片段版本（只做溯源，不进消息）
                    "prompt": rt.get("prompt") or {},
                    # 子代理 LLM 调用数（并行分支共享预算）：与父级 token 用量分开看
                    "subagent_llm_calls": int(
                        getattr(deps, "subagent_llm_calls", 0) or 0
                    ),
                    # 轮内纠偏补丁触发次数（P1-3）：按数据淘汰补丁，而不是只增不减
                    "patches": dict(rt.get("patches") or {}),
                    # 写后回读/答复核对的观测值：mutation 数 + 缺口事实数
                    "mutation_count": len(rt.get("mutation_ledger") or []),
                    "answer_lint_missing": rt.get("answer_lint_missing"),
                    # 消息链指纹（缓存前缀监测）：下一轮与本轮做前缀比对，
                    # 首个分歧位置即前缀命中断点
                    "chain_len": rt.get("chain_len"),
                    "chain_divergence": rt.get("chain_divergence"),
                    "chain_hashes": rt.get("chain_hashes") or [],
                    # 本轮范围/划选溯源（回放面板展示；只存元数据）
                    "kb_scope": _scope_meta(state),
                    "quote": _quote_meta(state),
                    # 子代理派发汇总：轮数/子任务数/模式/错误/降级
                    "dispatch": dispatch_meta,
                },
            )
            write_trace(
                settings,
                run.id,
                {
                    "conversation_id": conv_id,
                    "question": state["question"][:1000],
                    "plan": state.get("plan_steps") or [],
                    "tool_trace": tool_trace,
                    "answer_len": len(final_text),
                    "latency_ms": _compute_latency_ms(deps, rt),
                    "status": run_status,
                    "error": rt.get("error"),
                    "memory_hits": (state.get("memory_hits") or [])[:5],
                    "todos": state.get("todos") or [],
                    "plan_done_count": state.get("plan_done_count", 0),
                    "subagents": int(state.get("subagent_round_count") or 0),
                    "subagent_llm_calls": int(
                        getattr(deps, "subagent_llm_calls", 0) or 0
                    ),
                    "usage": usage_summary(),
                    "calls": rt.get("calls") or [],
                    "timings": dict(rt.get("timings") or {}),
                    "prompt": rt.get("prompt") or {},
                    # mutation 账本（只留前 20 条）：排查回读/答复核对问题
                    "mutation_ledger": (rt.get("mutation_ledger") or [])[:20],
                    "answer_lint_missing": rt.get("answer_lint_missing"),
                    "kb_scope": _scope_meta(state),
                    "quote": _quote_meta(state),
                    "dispatch": dispatch_meta,
                },
            )
        except Exception as exc:
            db.rollback()
            logger.warning("写入 Agent 运行记录失败：%s", exc)

    # 原生检查点裁剪：每线程只留最近 N 条快照
    # （每 superstep 一条含完整 messages 的快照，长会话会让 checkpoints.sqlite 单调增长；
    #   实测曾到 700MB+。收尾时无未完成任务，裁剪不会碰挂起中的线程。）
    if conv_id is not None:
        # 本轮已正常收尾：历史前缀缓存不再需要（挂起路径不会走到 finalize）
        try:
            getattr(service, "_pending_prefix", {}).pop(conv_id, None)
        except Exception:
            pass
        try:
            saver = deps.service.checkpoint_saver
            if saver is not None:
                from ...native_checkpoint import prune_thread

                keep = int(effective(settings, "checkpoint_keep_per_thread") or 20)
                prune_thread(saver, f"conv:{conv_id}", keep=keep)
        except Exception as exc:
            logger.warning("裁剪原生检查点失败：%s", exc)

    # 标题后置：后台已生成则推送给前端更新（未完成则由会话刷新兜底）
    title_thread = deps.title_thread
    if title_thread is not None and title_thread.is_alive():
        title_thread.join(timeout=1.0)
    if deps.title_holder:
        # 带上会话号：流式期间用户可能已切走，前端必须按 id 更新而不是当前会话
        bus.emit(
            "title",
            {"title": deps.title_holder[0], "conversation_id": conv_id},
        )

    # 附录作为 token 补发 + done
    extra_text = appendix + lint_appended
    if extra_text:
        bus.emit("token", extra_text)
    bus.emit(
        "done",
        {
            "ok": True,
            "stopped": rt.get("status") == "stopped",
            "sources": deduped,
            "tool_trace": tool_trace,
        },
    )

    # 长任务轨迹压缩（P1）：工具调用 >= 3 次时生成过程摘要，下次提问注入。
    # 放在 done **之后**，并且放到独立后台线程 + 独立短会话：它是一次 LLM
    # 调用，不能拖住生成器退出（生成器不退 → API 不释放会话锁 → 立即追问 409）。
    if (
        effective(settings, "trajectory_compress_enabled")
        and conv_id is not None
        and len(tool_trace) >= 3
    ):
        # 快照数据再起线程：节点返回后 state 可能被 LangGraph 复用，不能持有引用
        question_snapshot = state["question"]
        plan_snapshot = list(state.get("plan_steps") or [])
        trace_snapshot = list(tool_trace)

        def _compress_in_background() -> None:
            bg_db = None
            try:
                from ...db.database import SessionLocal, db_ready

                if not db_ready or SessionLocal is None:
                    return
                bg_db = SessionLocal()
                from ...trajectory import compress_trajectory

                service.rag.acquire_llm()
                try:
                    compress_trajectory(
                        service.context._invoke,
                        bg_db,
                        conv_id,
                        question_snapshot,
                        plan_snapshot,
                        trace_snapshot,
                    )
                finally:
                    service.rag.release_llm()
            except Exception as exc:
                logger.warning("轨迹压缩失败：%s", exc)
                if bg_db is not None:
                    try:
                        bg_db.rollback()
                    except Exception:
                        pass
            finally:
                if bg_db is not None:
                    try:
                        bg_db.close()
                    except Exception:
                        pass

        threading.Thread(
            target=_compress_in_background,
            daemon=True,
            name=f"trajectory-{conv_id}",
        ).start()
    return {}


# ============================================================
# 条件路由
# ============================================================


