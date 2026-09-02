"""Agent 图共享状态：AgentState / EventBus / 缓存键常量（自 langgraph_agent.py 拆分）。"""

from __future__ import annotations

import logging
import operator
import threading
from queue import Queue
from typing import Annotated, TypedDict

from sqlalchemy.orm import Session

from .agent import AgentService  # AgentState.service 注解：图构建时 get_type_hints 需要可解析（基类避免循环依赖）

logger = logging.getLogger(__name__)


class AgentState(TypedDict, total=False):
    """LangGraph 节点间共享的状态。"""

    service: "AgentService"
    bus: "EventBus"
    db: Session | None
    runtime: dict
    stop_event: threading.Event | None

    question: str
    images: list[str]
    use_web_search: bool
    use_knowledge_base: bool
    conversation_id: int | None
    template_id: int | None
    system_prompt: str | None
    tool_mode: str
    plan_only: bool
    resume_plan: list[str] | None

    messages: list
    tools: list
    pending_tool_calls: list
    sources: list[dict]
    tool_trace: list[dict]
    counter: list[int]
    tool_calls_used: int
    failure_count: int
    forced_final: bool
    last_call_warned: bool
    verify_fail_count: int
    early_created: bool
    task_mode: bool
    project_dir: str | None

    plan_steps: list[str]
    plan_map: list[dict]
    kb_documents: list[str]
    memory_hits: list[str]
    xml_retry_count: int
    memory_summary: str | None
    vision_descriptions: list[str]
    summary_text: str | None
    todos: list[dict]
    plan_done_count: int
    force_continue: bool
    plan_push_count: int
    sub_task: dict
    subagent_results: Annotated[list[dict], operator.add]
    dispatch_done: bool

    title_holder: list[str]
    title_thread: threading.Thread | None
    persist_start: int


class EventBus:
    """节点向调用方推送 SSE 事件的中转。"""

    def __init__(self, queue: Queue) -> None:
        self._queue = queue

    def emit(self, event: str, data) -> None:
        self._queue.put({"event": event, "data": data})


