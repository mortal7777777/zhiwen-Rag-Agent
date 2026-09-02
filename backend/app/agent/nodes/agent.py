"""agent 节点：LLM 并发锁内流式生成，工具轮判定（缓冲/XML 兜底），预算与强制收尾（自 langgraph_agent.py 拆分）。"""

from __future__ import annotations

import logging
import time


from ...config import Settings

logger = logging.getLogger(__name__)
from ..state import AgentState, EventBus
from .common import _iteration_limit
from .subagent import _remaining_needs_tools
from ..utils import (
    _XML_TOOL_MARKERS,
    _normalize_tool_markers,
    _parse_xml_tool_calls,
    _strip_xml_tool_tags,
)

from langchain_core.messages import AIMessage, SystemMessage
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..langgraph_agent import LangGraphAgentService  # 仅类型注解
from ...tracing import get_usage_collector

# ==== 函数体（原文）====
def _agent_node(state: AgentState) -> dict:
    service: LangGraphAgentService = state["service"]
    bus: EventBus = state["bus"]
    settings: Settings = service.settings
    runtime: dict = state["runtime"]
    messages = state["messages"]
    tools = state["tools"]
    stop_event = state.get("stop_event")

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    if stopped():
        runtime["status"] = "stopped"
        return {}

    # 还剩最后一次工具调用时提前提示：优先补充检索，随后必须作答
    remaining_calls = _iteration_limit(state, settings) - state["tool_calls_used"]
    if (
        tools
        and state.get("plan_steps")
        and remaining_calls == 1
        and not state["forced_final"]
        and not state["last_call_warned"]
    ):
        state["last_call_warned"] = True
        messages.append(
            SystemMessage(
                content=(
                    "你只剩最后一次工具调用机会。若信息仍不完整，"
                    "请利用这次机会做一次补充检索（优先 knowledge_base_search"
                    " 或 web_search）；之后必须给出最终回答。"
                    "若仍不足，明确告诉用户缺少什么，不要编造。"
                )
            )
        )

    # 思考摘要前置：正文输出前先展示"已深度思考"（有工具调用历史时）
    if (
        state["tool_calls_used"] > 0
        and settings.reasoning_summary_enabled
        and not runtime.get("reasoning_emitted")
    ):
        runtime["reasoning_emitted"] = True
        summary = ""
        evt = runtime.get("_reasoning")
        if evt is not None:
            # 摘要已在工具执行期间后台预生成，最多等 1s；超时不阻塞正文
            evt["done"].wait(timeout=1.0)
            summary = evt.get("summary") or ""
        if not summary:
            # 预生成未就绪：直接开始正文（摘要缺失不影响回答，避免额外延迟）
            summary = ""
        if summary:
            bus.emit("reasoning", {"summary": summary})

    bus.emit("status", {"phase": "thinking", "text": "正在思考并生成回答…"})

    # LLM 调用并发上限（生成阶段不占 GPU 锁）
    service.rag.acquire_llm()
    try:
        # 强制收尾也保持 bind_tools：tools 数组是 DeepSeek 缓存前缀的一部分
        # （实测同消息去/加 tools 命中从 768 掉到 640），去掉会让末次调用
        # 整链失效。若模型在 forced_final 下仍反复调工具，兜底 2 轮后
        # （forced_tool_rounds>=2）才解除绑定，保证终止。
        chat = (
            service.chat
            if not tools or runtime.get("forced_tool_rounds", 0) >= 2
            else service.chat.bind_tools(tools)
        )
        chunks: list = []
        stream = chat.stream(
            messages,
            config={"callbacks": [get_usage_collector()]},
        )
        # deepseek 等推理模型在工具调用轮次会先输出大量过渡思考文本
        # （"我将调用工具…"），与 tool_calls 同轮出现。这些文本不是最终
        # 回答，直接展示会污染对话。策略：流式时整体缓冲，流结束后基于
        # 合并文本统一判定：有 tool_calls 或 XML 工具调用标记 → 工具轮，
        # 缓冲文本整体丢弃（含过渡思考 + XML 标记，不展示给用户）；
        # 纯回答轮次才把缓冲文本作为正式回答发出。
        # 注意：判定必须放在合并文本上做——XML 标签（如 <tool_calls>）跨
        # 分片时逐片检测会漏判，导致工具调用信息泄漏进最终回答。
        buf_parts: list[str] = []
        for chunk in stream:
            if stopped():
                runtime["status"] = "stopped"
                break
            chunks.append(chunk)
            content = getattr(chunk, "content", None)
            if content:
                # TTFT 打点：首个内容 chunk 到达（含工具轮过渡文本，即 API 首字）
                timings = runtime.setdefault("timings", {})
                if "first_token_ms" not in timings:
                    timings["first_token_ms"] = round(
                        (time.perf_counter() - runtime["started"]) * 1000
                    )
                buf_parts.append(content)
    finally:
        service.rag.release_llm()

    if stopped():
        return {}
    if not chunks:
        # 模型没有输出（如空响应）：直接收尾，避免死循环
        state["pending_tool_calls"] = []
        return {
            "pending_tool_calls": [],
            "plan_done_count": state.get("plan_done_count", 0),
            "force_continue": False,
            "plan_push_count": state.get("plan_push_count", 0),
            "todos": state.get("todos") or [],
        }

    merged = chunks[0]
    for chunk in chunks[1:]:
        merged = merged + chunk
    usage = getattr(merged, "usage_metadata", None)
    if usage:
        runtime["token_usage"] = dict(usage)
        # 流式调用不在 on_llm_end 的 llm_output 里，手动补记到聚合统计
        get_usage_collector().add_usage(
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
        )
        get_usage_collector().add_cache_usage(dict(usage))
        # 逐调用遥测（P2）：定位轮内断链——哪次调用、命中多少 token
        runtime.setdefault("calls", []).append(
            {
                "in": int(usage.get("input_tokens") or 0),
                "out": int(usage.get("output_tokens") or 0),
                "read": int(
                    ((usage.get("input_token_details") or {}).get("cache_read"))
                    or 0
                ),
                "t_ms": round((time.perf_counter() - runtime["started"]) * 1000),
            }
        )

    tool_calls = list(getattr(merged, "tool_calls", None) or [])
    if state["forced_final"] and tool_calls:
        # forced_final 下模型仍调工具：计数，超过阈值后解除绑定兜底
        runtime["forced_tool_rounds"] = runtime.get("forced_tool_rounds", 0) + 1
    merged_text = merged.content or ""
    # 全角/DSML 前缀变体先归一化：否则 <｜DSML｜tool_calls> 这类标签
    # 漏过工具轮判定，正文（含标签）直接泄漏进最终回答
    merged_text = _normalize_tool_markers(merged_text)
    # ---- XML 风格工具调用兜底解析 ----
    # deepseek 等模型偶尔输出 <tool_calls><invoke name="bash">…</invoke></tool_calls>
    # 的 XML 格式（而非 OpenAI JSON tool_calls）。流式单 chunk 检测容易
    # 因标签被切分而漏判；这里对合并后的完整文本做兜底：
    # 1. 检测到 XML 工具调用标记 → 丢弃正文文本（不展示给用户）
    # 2. 尽力解析出 (工具名, 参数) 转成标准 tool_calls 执行
    if not tool_calls and any(m in merged_text for m in _XML_TOOL_MARKERS):
        parsed_xml_calls = _parse_xml_tool_calls(merged_text)
        if parsed_xml_calls:
            tool_calls = parsed_xml_calls
            logger.info("XML 工具调用兜底解析：%s", [tc.get("name") for tc in parsed_xml_calls])
    # 工具轮：content 是过渡思考文本，不写入消息历史（避免下一轮
    # 重复发送 + 污染上下文）；只保留 tool_calls 供 tools 节点执行
    if tool_calls:
        stored_content = ""
    elif any(m in merged_text for m in _XML_TOOL_MARKERS):
        # XML 标记存在但兜底解析失败：剥离标记再存，避免 <tool_calls>
        # 泄漏进消息历史（下轮会重复发送给模型）
        stored_content = _strip_xml_tool_tags(merged_text).strip()
    else:
        stored_content = merged_text
    # DeepSeek 思考模式：带 tool_calls 的 assistant 消息必须把
    # reasoning_content 原样回传（**空字符串也要回传空串**），否则 API 报
    # "The reasoning_content in the thinking mode must be passed back to
    # the API"。约 59% 工具轮的 reasoning 为空串，用 if reasoning 判断会
    # 丢掉字段 → 间歇性 400。这里无条件写入（模型未输出时用空串）。
    reasoning = (getattr(merged, "additional_kwargs", {}) or {}).get(
        "reasoning_content"
    )
    messages.append(
        AIMessage(
            content=stored_content,
            tool_calls=tool_calls,
            # 仅工具轮（带 tool_calls）必须回传 reasoning_content
            additional_kwargs=(
                {
                    "reasoning_content": (
                        reasoning if reasoning is not None else ""
                    )
                }
                if tool_calls
                else {}
            ),
        )
    )
    state["pending_tool_calls"] = tool_calls

    # ---- 工具轮判定（基于合并后的完整文本，跨分片 XML 标签不漏判）----
    # 有 tool_calls 或 XML 工具调用标记 → 工具轮：缓冲文本（过渡思考 +
    # XML 标记文本）整体丢弃，不展示给用户；纯回答轮才发出缓冲文本。
    is_tool_round = bool(tool_calls) or any(
        m in merged_text for m in _XML_TOOL_MARKERS
    )
    if not is_tool_round and buf_parts:
        # 逐片清理 XML 工具调用标记：sensenova 等模型偶尔把
        # <tool_calls> 写进正文，直接展示会泄漏
        text = _strip_xml_tool_tags("".join(buf_parts))
        runtime["final_text"] += text
        bus.emit("token", text)

    # ---- XML 标记存在但兜底解析失败：有界重试，避免"静默停止" ----
    # 模型把工具调用写成 XML 文本且格式无法解析时，不执行工具也不作答就
    # 收尾会表现为"停止生成"；提示模型改用标准工具调用或直接作答，最多重试 2 次。
    xml_force = False
    if (
        not tool_calls
        and any(m in merged_text for m in _XML_TOOL_MARKERS)
        and not state["forced_final"]
        and int(state.get("xml_retry_count") or 0) < 2
    ):
        state["xml_retry_count"] = int(state.get("xml_retry_count") or 0) + 1
        messages.append(
            SystemMessage(
                content=(
                    "你上一条输出包含 XML 风格工具调用标记（如 <tool_calls>/<invoke>），"
                    "未能解析执行。请改用标准工具调用格式继续（不要把工具调用写成 XML 文本），"
                    "或直接给出最终回答。"
                )
            )
        )
        xml_force = True

    # ---- 计划硬约束：还有未完成的工具型步骤时，不允许提前收尾 ----
    force_continue = False
    if (
        not tool_calls
        and not state["forced_final"]
        and runtime.get("status") != "stopped"
        and int(state.get("plan_push_count") or 0) < 3
    ):
        try:
            from ...todos import plan_progress

            todos = state.get("todos") or []
            done, _total, remaining = (
                plan_progress(todos) if todos else (0, 0, [])
            )
            if not todos and state.get("plan_map"):
                # 任务清单不可用（如 DB 降级）时退回计划步骤顺序
                done = min(
                    int(state.get("plan_done_count") or 0),
                    len(state["plan_map"]),
                )
                remaining = state["plan_map"][done:]
            remaining_hint = _remaining_needs_tools(remaining)
            if remaining_hint:
                force_continue = True
                state["plan_push_count"] = (
                    int(state.get("plan_push_count") or 0) + 1
                )
                remaining_text = "；".join(
                    str(item.get("text") or item.get("step") or item)
                    for item in remaining[:6]
                )
                messages.append(
                    SystemMessage(
                        content=(
                            "【计划硬约束】以下步骤尚未完成且需要工具："
                            f"{remaining_text}\n"
                            "请继续调用对应工具完成这些步骤，不要提前输出最终回答；"
                            "若某步确实无需执行，先用 todo_update remove 删除它，"
                            "或用 todo_update complete 标记为已完成，再结束。"
                        )
                    )
                )
        except Exception as exc:
            logger.warning("计划硬约束判断失败：%s", exc)
            force_continue = False

    return {
        "messages": messages,
        "pending_tool_calls": tool_calls,
        "tool_calls_used": state["tool_calls_used"],
        "forced_final": state["forced_final"],
        "last_call_warned": state["last_call_warned"],
        "plan_done_count": state.get("plan_done_count", 0),
        "force_continue": force_continue or xml_force,
        "plan_push_count": state.get("plan_push_count", 0),
        "xml_retry_count": state.get("xml_retry_count", 0),
        "todos": state.get("todos") or [],
    }


# ============================================================
# 节点：tools（执行工具 + 回填）
# ============================================================


