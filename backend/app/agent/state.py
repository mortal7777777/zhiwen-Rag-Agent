"""Agent 图共享状态：AgentState / Deps / EventBus / 归约器（自 langgraph_agent.py 拆分）。

两类东西分得很清：
- **AgentState**：会被 checkpointer 持久化的数据（消息链、计数器、清单、计划…），
  必须全部可序列化；
- **Deps**：请求级"活对象"（模型句柄、SSE 队列、DB Session、线程、工具对象），
  经 LangGraph Runtime context（`graph.invoke(..., context=Deps(...))`）注入节点，
  不进检查点——这也是"原生检查点里不再出现 __unserializable__ 占位符"的前提。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from queue import Queue
from types import SimpleNamespace
from typing import Annotated, TypedDict

from sqlalchemy.orm import Session

from .agent import AgentService  # Deps.service 注解：图构建时 get_type_hints 需要可解析（基类避免循环依赖）

logger = logging.getLogger(__name__)

# 子代理结果的"整轮清空"标记：reducer 通道在 checkpointer 下跨轮只增不减
# （输入传 [] 也会被 add 成"旧值 + []"），所以重置必须显式走一次归约。
RESULTS_RESET = "__reset__"


def merge_subagent_results(left: list | None, right: list | None) -> list:
    """subagent_results 归约器：并行分支追加 + 支持整轮清空。

    prepare 每轮返回 [RESULTS_RESET] 清空通道，于是：
    - 上一轮的子代理结论不会在本轮被重新合并（旧实现靠跨轮游标兜，游标
      一旦落后就会把过期结果当新结果回填，call_id 还对不上当轮声明）；
    - 长会话下通道不再无限增长（每个 superstep 的快照都要带它）。
    """
    if right and right[0] == RESULTS_RESET:
        return list(right[1:])
    return list(left or []) + list(right or [])


@dataclass
class Deps:
    """请求级依赖（活对象）：经 Runtime context 注入，不进检查点。

    这些都不可/不该序列化：模型句柄、SSE 事件队列、DB Session、停止信号、
    后台线程、LangChain 工具对象（带闭包）。放进 state 会让原生检查点只能
    存 `__unserializable__` 占位符，续跑/时间旅行也就无从谈起。
    """

    service: "AgentService"
    bus: "EventBus"
    db: Session | None = None
    stop_event: threading.Event | None = None
    # prepare 构建、agent/tools 节点使用（同一 run 内共享，靠 context 对象引用传递）
    tools: list = field(default_factory=list)
    # 请求级工具契约快照（mutating/回读/答复字段）。与 tools 同源但独立拷贝，
    # 避免并发请求共享可变注册表；不进检查点。
    tool_contracts: dict = field(default_factory=dict)
    # 新会话标题：后台线程生成，写入 title_holder，finalize 读取后发 title 事件
    title_holder: list[str] = field(default_factory=list)
    title_thread: threading.Thread | None = None
    # 子代理 LLM 调用预算：并行 Send 分支共享同一 Deps（请求级活对象）。
    # 计数器/锁不能放 state（要进检查点，锁不可序列化）。
    subagent_llm_calls: int = 0
    subagent_lock: threading.Lock = field(default_factory=threading.Lock)
    # 本段执行的起点与历史累计耗时（毫秒）。request-scoped、不进检查点：
    # 挂起时把本段耗时累计到 service 的内存注册表，恢复后从新段起点重新计时，
    # 避免把人工审批等待算进 latency_ms，也避免跨进程 perf_counter 基准失效。
    started: float = 0.0
    elapsed_ms: int = 0
    # 历史前缀（只读 DB 窗口）：不进检查点。state["messages"] 只保留
    # [静态核心, 静态动态块, D 块, 问题, 工具轮]，模型调用时按
    # state["history_insert_at"] 插入这段历史。
    prefix_messages: list = field(default_factory=list)


def runtime_of(deps: "Deps") -> SimpleNamespace:
    """图外直接调用节点时用的 runtime 替身（节点只读 `runtime.context`）。

    用途：异常兜底里 worker 直接调 `_finalize_node`；测试直调节点。
    """
    return SimpleNamespace(context=deps)


class AgentState(TypedDict, total=False):
    """LangGraph 节点间共享的**可序列化**状态（会进检查点）。"""

    runtime: dict

    question: str
    images: list[str]
    attachments: list[dict]
    use_web_search: bool
    use_knowledge_base: bool
    conversation_id: int | None
    template_id: int | None
    system_prompt: str | None
    tool_mode: str
    plan_only: bool

    messages: list
    pending_tool_calls: list
    pending_subtasks: list[dict]
    sources: list[dict]
    tool_trace: list[dict]
    counter: list[int]
    tool_calls_used: int
    failure_count: int
    forced_final: bool
    last_call_warned: bool
    verify_fail_count: int
    verify_env_warned: bool
    early_created: bool
    task_mode: bool
    project_dir: str | None

    plan_steps: list[str]
    plan_map: list[dict]
    # 本轮限定的知识库检索范围（相对路径）；空 = 全库
    kb_scope_paths: list[str]
    kb_scope_label: str
    # 查看器划选的原文 {text, context, source, page}（直接进上下文）；空 = 无引用
    quote: dict
    kb_documents: list[str]
    memory_hits: list[str]
    xml_retry_count: int
    empty_retry_count: int
    # 空正文重试的"下一轮用无思考模型"标记（跨 superstep 传递，必须进检查点）
    empty_retry_pending: bool
    memory_summary: str | None
    vision_descriptions: list[str]
    summary_text: str | None
    todos: list[dict]
    plan_done_count: int
    plan_progress_sig: str
    force_continue: bool
    plan_push_count: int
    # 计划模式"无工具调用收尾→推回提交"的计数（上限见 agent._PLAN_SUBMIT_PUSH_LIMIT）
    plan_submit_push_count: int
    sub_task: dict
    subagent_results: Annotated[list[dict], merge_subagent_results]
    subagent_round_count: int
    dispatch_rounds: int

    # 本轮"问题之后"消息在链中的起点：finalize 据此落库（纯数据，续跑时要能读到）
    persist_start: int
    # 历史前缀在模型消息里的插入位置（= 静态核心+静态动态块条数）；
    # state["messages"] 不含历史，续跑时按它插回 Deps.prefix_messages。
    history_insert_at: int
    # 历史窗口对应的 DB 行 id：时间线展示/回滚时据此重建历史前缀。
    history_row_ids: list[int]

    # D 块版本账本（键 → 最后一次注入的内容哈希）：区分"A 就是最新版本"与
    # "A 之上还注入过更新的 B"（A→B→A 状态回退时靠它决定重新注入）。
    # 与消息同在一个检查点快照里，天然同生共死；丢失只会多注入一次，不会漏。
    dblock_versions: dict[str, str]


class EventBus:
    """节点向调用方推送 SSE 事件的中转。

    queue：CLI/生成器模式的阻塞队列；
    sink：SSE 直连模式的回调（通常是 loop.call_soon_threadsafe 包装的
    asyncio.Queue 投递），省掉一层桥接线程与队列拷贝。
    """

    def __init__(self, queue: Queue | None = None, sink=None) -> None:
        self._queue = queue
        self._sink = sink

    def emit(self, event: str, data) -> None:
        item = {"event": event, "data": data}
        if self._sink is not None:
            self._sink(item)
        elif self._queue is not None:
            self._queue.put(item)


