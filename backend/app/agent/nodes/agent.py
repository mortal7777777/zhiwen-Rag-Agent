"""agent 节点：LLM 并发锁内流式生成，工具轮判定（缓冲/XML 兜底），预算与强制收尾（自 langgraph_agent.py 拆分）。"""

from __future__ import annotations

import logging
import queue as _queue
import threading as _threading
import time


from ...config import Settings
from ...llm_text import extract_thinking_blocks, message_text
from ...runtime_config import effective

logger = logging.getLogger(__name__)
from ..state import AgentState, EventBus
from .common import _iteration_limit
from .subagent import (
    _parse_declared_subtasks,
    _remaining_needs_tools,
    _split_declaration,
)
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
def _render_plan_text(approval: dict) -> str:
    """把提交的计划渲染成给用户看的正文（不再让模型多说一轮）。"""
    steps = approval.get("steps") or []
    lines = [f"计划已就绪（{len(steps)} 步），确认或修改后即按此执行："]
    lines.extend(f"{i}. {s}" for i, s in enumerate(steps, 1))
    summary = str(approval.get("summary") or "").strip()
    if summary:
        lines.append(f"\n{summary}")
    lines.append("\n（在计划卡片上点「确认执行」开始；也可以先改步骤。）")
    return "\n".join(lines)



_STREAM_DONE = object()
_DEFAULT_STALL_TIMEOUT_S = 150.0
# 空正文重试上限：思考模式下一次 32000 输出预算全被 reasoning 吃光并不罕见
# （2026-09-24 实测），只给一次机会太紧；重试轮本身已改用无思考模型。
_EMPTY_RETRY_LIMIT = 2


def _should_retry_empty(
    *,
    tool_calls: list,
    merged_text: str,
    usage: dict | None,
    forced_final: bool,
    status: str | None,
    retry_count: int,
    limit: int = _EMPTY_RETRY_LIMIT,
) -> bool:
    """是否给一次"带明确指令的空正文重试"。

    命中条件：本轮没有工具调用、正文为空、不是强制收尾、且模型**确实产生过
    输出**（output_tokens>0，说明是被长度上限截断而非真空响应）。
    重试次数受 limit 约束，且计数需每轮重置（见 prepare/langgraph_agent 的
    empty_retry_count 初始化，checkpointer 会把 state 跨轮带下去）。
    """
    if tool_calls or forced_final or status == "stopped":
        return False
    if merged_text.strip():
        return False
    if any(marker in merged_text for marker in _XML_TOOL_MARKERS):
        return False
    if int((usage or {}).get("output_tokens") or 0) <= 0:
        return False
    return retry_count < limit


class _StreamError:
    """守护线程捕获的传输异常。"""

    __slots__ = ("exc",)

    def __init__(self, exc: Exception) -> None:
        self.exc = exc


def _stall_timeout(settings: Settings) -> float:
    raw = getattr(settings, "agent_llm_stall_timeout_s", None)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = _DEFAULT_STALL_TIMEOUT_S
    return value if value > 0 else _DEFAULT_STALL_TIMEOUT_S


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

    # 计划已提交：不再调模型（实测提交后还会再多跑两轮共 4 分钟），
    # 直接把计划渲染成正文收尾，把回合让给用户确认
    approval = runtime.get("plan_approval") or {}
    if approval.get("steps"):
        text = _render_plan_text(approval)
        runtime["final_text"] = (runtime.get("final_text") or "") + text
        bus.emit("token", text)
        return {
            "pending_tool_calls": [],
            "pending_subtasks": [],
            "force_continue": False,
            "plan_done_count": state.get("plan_done_count", 0),
            "todos": state.get("todos") or [],
            "plan_steps": state.get("plan_steps") or [],
            "plan_map": state.get("plan_map") or [],
        }

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
        and effective(settings, "reasoning_summary_enabled", False)
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
        # 空正文重试轮改用无思考模型：reasoning 与正文共用输出预算，
        # 上一次就是被思考吃光的，重试再开思考大概率重演（2026-09-24 实测）。
        thinking_off = bool(state.get("empty_retry_pending"))
        if thinking_off:
            state["empty_retry_pending"] = False
        model = service.chat_plain if thinking_off else service.chat
        chat = (
            model
            if not tools or runtime.get("forced_tool_rounds", 0) >= 2
            else model.bind_tools(tools)
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
        # ---- 卡死看门狗 ----
        # 直接 `for chunk in stream` 阻塞在 socket 读上时无法被中断：httpx 的
        # read timeout 只限制单次 socket 读，服务端持续发 SSE 心跳就永不触发，
        # SDK 还会静默重发（max_retries=2）。2026-09-24 实测末次调用 606s 只
        # 产出 1 个 token、日志无完成记录，用户只看到"没有回答"。
        # 这里把消费放到守护线程，主线程按 chunk 间隔判定：超过阈值即报错收尾。
        # 判定按**任意 chunk**计时（思考期也在流式输出），不看正文是否为空。
        stall_s = _stall_timeout(settings)
        stream_queue: "_queue.Queue" = _queue.Queue(maxsize=256)

        def _producer() -> None:
            try:
                for chunk in stream:
                    try:
                        stream_queue.put(chunk, timeout=1.0)
                    except _queue.Full:
                        # 消费者已放弃（卡死/停止）：收尾并关闭流，避免继续挂连接
                        break
            except Exception as exc:  # 传输异常也要变成用户可见错误
                try:
                    stream_queue.put(_StreamError(exc), timeout=1.0)
                except _queue.Full:
                    pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass
                try:
                    stream_queue.put(_STREAM_DONE, timeout=1.0)
                except _queue.Full:
                    pass

        _threading.Thread(target=_producer, daemon=True).start()
        stream_error: Exception | None = None
        stalled = False
        while True:
            if stopped():
                runtime["status"] = "stopped"
                break
            try:
                item = stream_queue.get(timeout=stall_s)
            except _queue.Empty:
                stalled = True
                break
            if item is _STREAM_DONE:
                break
            if isinstance(item, _StreamError):
                stream_error = item.exc
                break
            chunk = item
            chunks.append(chunk)
            delta = message_text(getattr(chunk, "content", None))
            if delta:
                # TTFT 打点：首个内容 chunk 到达（含工具轮过渡文本，即 API 首字）
                timings = runtime.setdefault("timings", {})
                if "first_token_ms" not in timings:
                    timings["first_token_ms"] = round(
                        (time.perf_counter() - runtime["started"]) * 1000
                    )
                buf_parts.append(delta)
    finally:
        service.rag.release_llm()

    if stalled:
        runtime["status"] = "error"
        runtime["error"] = (
            f"模型响应卡死：连续 {stall_s:.0f} 秒未收到任何输出，已中断本轮。"
            "请重试；若反复出现，检查网络/代理或供应商状态。"
        )
        bus.emit(
            "error",
            {"message": runtime["error"], "phase": "llm", "code": "llm_stall"},
        )
        if stop_event is not None:
            stop_event.set()
        state["pending_tool_calls"] = []
        return {
            "pending_tool_calls": [],
            "plan_done_count": state.get("plan_done_count", 0),
            "force_continue": False,
            "plan_push_count": state.get("plan_push_count", 0),
            "empty_retry_pending": False,
            "todos": state.get("todos") or [],
        }
    if stream_error is not None:
        runtime["status"] = "error"
        runtime["error"] = (
            f"模型调用中断：{type(stream_error).__name__}: {stream_error}"
        )
        bus.emit(
            "error",
            {
                "message": runtime["error"],
                "phase": "llm",
                "code": "llm_stream_error",
            },
        )
        if stop_event is not None:
            stop_event.set()
        state["pending_tool_calls"] = []
        return {
            "pending_tool_calls": [],
            "plan_done_count": state.get("plan_done_count", 0),
            "force_continue": False,
            "plan_push_count": state.get("plan_push_count", 0),
            "empty_retry_pending": False,
            "todos": state.get("todos") or [],
        }

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
            "empty_retry_pending": False,
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
    merged_text = message_text(merged.content)
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
    # ---- 派发声明：从工具调用里摘出来，交给图条件边扇出（不走 tools 节点）----
    # 消息形状仍复用 tool_calls（思考模式 reasoning 回传、缓存前缀形状统一），
    # 但执行路径是 dispatch → Send 并行 → merge 回填 ToolMessage。
    declared, others = _split_declaration(tool_calls)
    exec_calls = tool_calls
    dispatch_cap = int(effective(settings, "agent_max_dispatch_rounds") or 3)
    if declared and int(state.get("dispatch_rounds") or 0) >= dispatch_cap:
        # 达上限：不再受理声明，让它作为普通工具调用走 tools 节点（链形状合法、
        # 该工具无副作用），并明确要求模型自己收尾，避免无界扇出。
        declared = []
        messages.append(
            SystemMessage(
                content=(
                    f"本轮派发已达上限（{dispatch_cap} 轮），不再受理新的派发声明。"
                    "请自己完成剩余工作或直接作答。"
                )
            )
        )
    if declared:
        subtasks, downgraded = _parse_declared_subtasks(declared)
        if not subtasks:
            # 解析不出任何子任务（空 task 等）：当作普通工具调用走 tools 节点，
            # 由它回一条结果——否则这条 tool_call 无人应答，链上就断了配对
            declared = []
        else:
            # 消息里只保留声明调用：协议要求一个 tool_call 对应一条结果，
            # 而声明的结果由 merge 回填；混进来的普通调用下一轮重发。
            tool_calls = declared
            exec_calls = []
            state["pending_subtasks"] = subtasks
            if others:
                messages.append(
                    SystemMessage(
                        content=(
                            "本轮已受理派发声明，同轮的其它工具调用未执行"
                            f"（{', '.join(str((tc or {}).get('name') or '') for tc in others)}）。"
                            "子任务结果返回后，如仍需这些调用请重新发起。"
                        )
                    )
                )
            if downgraded:
                messages.append(
                    SystemMessage(
                        content=(
                            f"本轮声明了多个 execute 子任务，只放开第一个；"
                            f"其余 {downgraded} 个已按 research 只读执行"
                            "（并行写同一目录有冲突风险）。需要串行执行请下一轮再派。"
                        )
                    )
                )
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
    # Anthropic 格式（DeepSeek /anthropic 等）的思考不在 additional_kwargs 里，
    # 而是 content 里的 thinking 块；工具轮必须把它原样回传（signature 也要），
    # 否则端点报 "The content[].thinking ... must be passed back"（2026-09-26 实测）。
    thinking_blocks = extract_thinking_blocks(getattr(merged, "content", None))
    extra_kwargs: dict = {}
    if tool_calls:
        extra_kwargs["reasoning_content"] = (
            reasoning if reasoning is not None else ""
        )
        if thinking_blocks:
            extra_kwargs["anthropic_thinking"] = thinking_blocks
    messages.append(
        AIMessage(
            content=stored_content,
            tool_calls=tool_calls,
            additional_kwargs=extra_kwargs,
        )
    )
    state["pending_tool_calls"] = exec_calls

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

    # ---- 空正文兜底：输出预算被思考吃光，正文一个字都没剩 ----
    # 判定抽成纯函数（_should_retry_empty）便于单测；重试轮会改用无思考
    # 模型（chat_plain），否则重试可能再次把预算全花在 reasoning 上。
    empty_force = False
    if _should_retry_empty(
        tool_calls=tool_calls,
        merged_text=merged_text,
        usage=usage,
        forced_final=bool(state["forced_final"]),
        status=runtime.get("status"),
        retry_count=int(state.get("empty_retry_count") or 0),
    ):
        state["empty_retry_count"] = int(state.get("empty_retry_count") or 0) + 1
        # 下一轮用无思考模型：只有一次机会，不能再被 reasoning 吃光
        state["empty_retry_pending"] = True
        messages.append(
            SystemMessage(
                content=(
                    "你上一条输出没有产生任何正文（内容可能全部消耗在思考上、"
                    "并被输出长度上限截断）。请直接输出给用户的回答正文，"
                    "不要再重复思考；若信息确实不足，明确说明缺少什么。"
                )
            )
        )
        empty_force = True

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
        "pending_tool_calls": exec_calls,
        "tool_calls_used": state["tool_calls_used"],
        "forced_final": state["forced_final"],
        "last_call_warned": state["last_call_warned"],
        "plan_done_count": state.get("plan_done_count", 0),
        "force_continue": force_continue or xml_force or empty_force,
        "plan_push_count": state.get("plan_push_count", 0),
        "xml_retry_count": state.get("xml_retry_count", 0),
        "empty_retry_count": state.get("empty_retry_count", 0),
        "empty_retry_pending": bool(state.get("empty_retry_pending")),
        # 派发声明必须经返回值写回（节点内直接改 state 不生效，历史大坑）
        "pending_subtasks": state.get("pending_subtasks") or [],
        "todos": state.get("todos") or [],
    }


# ============================================================
# 节点：tools（执行工具 + 回填）
# ============================================================


