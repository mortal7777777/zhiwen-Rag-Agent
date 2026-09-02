"""finalize 节点：来源去重/联网附录/消息持久化/运行记录与 trace/标题后置（自 langgraph_agent.py 拆分）。"""

from __future__ import annotations

import json
import logging
import re
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

logger = logging.getLogger(__name__)
from ..state import AgentState, EventBus
from .common import _plan_hint
from ..utils import _strip_xml_tool_tags

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..langgraph_agent import LangGraphAgentService  # 仅类型注解
from ...tracing import usage_summary, usage_summary_with_aux, write_trace

# ==== 函数体（原文）====
def _finalize_node(state: AgentState) -> dict:
    service: LangGraphAgentService = state["service"]
    bus: EventBus = state["bus"]
    db: Session | None = state["db"]
    settings: Settings = service.settings
    runtime: dict = state["runtime"]

    final_text = runtime.get("final_text") or ""
    # 收尾清理：完整文本上再清一次 XML 工具标记 + strip（分片清理可能漏跨片标签）
    cleaned_final = _strip_xml_tool_tags(final_text).strip()
    if cleaned_final != final_text:
        final_text = cleaned_final
        runtime["final_text"] = cleaned_final
    sources = state.get("sources") or []
    tool_trace = state.get("tool_trace") or []
    conv_id = runtime.get("conv_id")

    # 收尾同步任务清单：纯推理/总结类步骤视为被最终回答覆盖，自动补完成；
    # 工具型步骤若仍未完成则保留未勾选状态（审计留痕，不假装完成）
    if db is not None and conv_id is not None and runtime.get("status") == "ok":
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

    # 来源去重（按 index 去重，保证 [n] 编号与来源卡片一一对应）
    deduped: list[dict] = []
    seen_src: set[str] = set()
    for item in sources:
        key = str(item.get("index") or item.get("content", ""))
        if key and key not in seen_src:
            seen_src.add(key)
            deduped.append(item)
    deduped = deduped[:8]

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
            runtime["final_text"] = final_text

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
                    repo.add_message(
                        db,
                        conv_id,
                        "assistant",
                        json.dumps(marker, ensure_ascii=False),
                    )
                # 无 tool_calls 的 AIMessage（最终回答）单独落库，这里跳过
            if not q_inserted:
                repo.add_message(db, conv_id, "user", state["question"])
            if runtime.get("status") != "stopped" and final_text:
                repo.add_message(
                    db,
                    conv_id,
                    "assistant",
                    final_text,
                    tool_trace=tool_trace,
                    sources=deduped,
                )
        except Exception as exc:
            logger.warning("保存对话消息失败：%s", exc)

    # 决策运行记录 + 结构化 trace（可观测性）
    if db is not None:
        try:
            run_status = (
                "stopped"
                if runtime.get("status") == "stopped"
                else "error"
                if runtime.get("error")
                else "ok"
            )
            run = repo.create_agent_run(
                db,
                conversation_id=conv_id,
                question=state["question"],
                plan=state.get("plan_steps") or [],
                tool_trace=tool_trace,
                answer_len=len(final_text),
                latency_ms=round((time.perf_counter() - runtime["started"]) * 1000),
                status=run_status,
                error=runtime.get("error"),
                token_usage={
                    **usage_summary_with_aux(),
                    "last_call": runtime.get("token_usage"),
                    # 逐调用遥测：input/output/cache_read，定位轮内断链
                    "calls": runtime.get("calls") or [],
                    # 分阶段耗时：prepare/dispatch/subagent/merge/agent/tools/finalize + TTFT
                    "timings": dict(runtime.get("timings") or {}),
                    # 工具定义哈希（缓存前缀监测）：与上一 run 对比，
                    # 变化说明前缀被破坏，命中率可能下降
                    "tools_hash": runtime.get("tools_hash"),
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
                    "latency_ms": round(
                        (time.perf_counter() - runtime["started"]) * 1000
                    ),
                    "status": run_status,
                    "error": runtime.get("error"),
                    "memory_hits": (state.get("memory_hits") or [])[:5],
                    "todos": state.get("todos") or [],
                    "plan_done_count": state.get("plan_done_count", 0),
                    "subagents": len(state.get("subagent_results") or []),
                    "usage": usage_summary(),
                    "calls": runtime.get("calls") or [],
                    "timings": dict(runtime.get("timings") or {}),
                },
            )
        except Exception as exc:
            logger.warning("写入 Agent 运行记录失败：%s", exc)

    # 长任务轨迹压缩（P1）：工具调用 >= 3 次时生成过程摘要，下次提问注入
    if (
        settings.trajectory_compress_enabled
        and db is not None
        and conv_id is not None
        and len(tool_trace) >= 3
    ):
        try:
            from ..trajectory import compress_trajectory

            compress_trajectory(
                service.context._invoke,
                db,
                conv_id,
                state["question"],
                state.get("plan_steps") or [],
                tool_trace,
            )
        except Exception as exc:
            logger.warning("轨迹压缩失败：%s", exc)

    # checkpoint：正常/停止收尾后清除快照
    if settings.checkpoint_enabled and db is not None and conv_id is not None:
        try:
            from ...checkpoint import get_store

            get_store(settings).clear(conv_id)
        except Exception as exc:
            logger.warning("清除 checkpoint 失败：%s", exc)

    # 标题后置：后台已生成则推送给前端更新（未完成则由会话刷新兜底）
    title_thread = state.get("title_thread")
    if title_thread is not None and title_thread.is_alive():
        title_thread.join(timeout=1.0)
    if state.get("title_holder"):
        bus.emit("title", {"title": state["title_holder"][0]})

    # 附录作为 token 补发 + done
    if appendix:
        bus.emit("token", appendix)
    bus.emit(
        "done",
        {
            "ok": True,
            "stopped": runtime.get("status") == "stopped",
            "sources": deduped,
            "tool_trace": tool_trace,
        },
    )
    return {}


# ============================================================
# 条件路由
# ============================================================


