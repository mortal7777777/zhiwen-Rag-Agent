"""LangGraphAgentService：LangGraph 编排的服务入口（图构建 / 事件流 / 运行记录）。

2026-08-27 模块化重构：图定义、节点实现、工具辅助已拆分到
graph.py / nodes/ / utils.py / state.py，本文件仅保留服务类。
"""

from __future__ import annotations

import json
import logging
import operator
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime
from queue import Queue
from typing import Annotated, TypedDict
from zoneinfo import ZoneInfo

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import repository as repo
from ..rag.service import RAGService
from ..runtime_config import effective
from ..tracing import get_aux_usage_collector, get_usage_collector, reset_usage, usage_summary, usage_summary_with_aux, write_trace
from .agent import AgentService
from .graph import build_agent_graph
from .nodes.finalize import _finalize_node
from .prompts import compose_system_prompt
from .state import AgentState, EventBus

logger = logging.getLogger(__name__)

class LangGraphAgentService(AgentService):
    """LangGraph 编排版 Agent 服务：接口与 AgentService 完全一致。"""

    def __init__(self, settings: Settings, rag_service: RAGService):
        super().__init__(settings, rag_service)
        self._graph = None
        self._mcp = None
        self._checkpoint_saver = None
        self._checkpoint_conn = None
        self.recursion_limit = getattr(settings, "agent_recursion_limit", 30)

    @property
    def graph(self):
        if self._graph is None:
            saver = self.checkpoint_saver
            self._graph = build_agent_graph(checkpointer=saver)
        return self._graph

    @property
    def checkpoint_saver(self):
        """原生 checkpointer（失败时返回 None，不影响运行）。"""
        if not effective(self.settings, "checkpoint_native_enabled"):
            return None
        if self._checkpoint_saver is None:
            try:
                from ..native_checkpoint import build_saver

                self._checkpoint_saver, self._checkpoint_conn = build_saver(
                    self.settings.meta_dir
                )
            except Exception as exc:
                logger.warning("原生 checkpointer 初始化失败，降级为无快照：%s", exc)
                self._checkpoint_saver = None
        return self._checkpoint_saver

    def refresh(self) -> None:
        """设置变更后重置懒加载缓存；图结构不变，无需重建。"""
        super().refresh()
        self._mcp = None

    # ---------------- 扩展工具（P0：MCP / 受控执行）----------------

    def mcp_tools(self, db: Session | None) -> list:
        """加载启用中的 MCP 服务器工具（连接失败自动降级跳过）。"""
        if not effective(self.settings, "mcp_enabled", True):
            return []
        if self._mcp is None:
            from ..mcp_manager import MCPManager

            self._mcp = MCPManager()
        servers = self._load_mcp_servers(db)
        if not servers:
            return []
        try:
            return self._mcp.configure(servers)
        except Exception as exc:
            logger.warning("MCP 工具配置失败：%s", exc)
            return []

    def extra_tools(
        self, project_dir: str | None = None, sandbox: str | None = None
    ) -> list:
        """受控执行工具：类 Claude Code 文件/命令工具集（需在设置中开启总开关）。

        sandbox：请求级沙箱覆盖（subprocess|docker），空则跟随设置页。
        """
        if not effective(self.settings, "advanced_tools_enabled", True):
            return []
        try:
            from ..tools_extra import make_agent_tools

            return make_agent_tools(self.settings, project_dir, sandbox)
        except Exception as exc:
            logger.warning("受控执行工具加载失败：%s", exc)
            return []

    @staticmethod
    def _load_mcp_servers(db: Session | None) -> list[dict]:
        if db is None:
            return []
        try:
            raw = repo.get_meta(db, "mcp_servers")
            if raw:
                data = json.loads(raw)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []

    # ---------------- 入口（签名与 AgentService._run 一致）----------------

    def _run(
        self,
        question: str,
        use_web_search: bool,
        use_knowledge_base: bool,
        conversation_id: int | None,
        template_id: int | None,
        system_prompt: str | None,
        db: Session | None,
        tool_mode: str,
        stop_event: threading.Event | None,
        images: list[str] | None,
        plan_only: bool = False,
        resume_plan: list[str] | None = None,
        project_dir: str | None = None,
        command_sandbox: str | None = None,
    ):
        """执行 LangGraph 编排，逐事件产出（session/plan/vision/.../done）。"""
        settings = self.settings
        images = [img for img in (images or []) if img and img.strip()][
            : settings.vision_max_images
        ]

        # 工具开关解析：tool_mode 优先，兼容旧前端布尔开关
        use_knowledge_base, use_web_search = self._resolve_tool_flags(
            tool_mode, use_web_search, use_knowledge_base
        )

        # 新会话先建号：让原生 checkpointer 的 thread_id 从第一轮就能对齐 conv:id
        early_created = False
        if conversation_id is None and db is not None:
            try:
                conv = repo.create_conversation(
                    db,
                    title="新对话",
                    template_id=template_id or None,
                    project_dir=project_dir,
                )
                conversation_id = conv.id
                early_created = True
            except Exception as exc:
                logger.warning("提前创建会话失败：%s", exc)
        elif conversation_id is not None and db is not None:
            # 已有会话：CLI 未传 project_dir 时，恢复会话保存的工作目录
            try:
                conv = repo.get_conversation(db, conversation_id)
                if conv is not None:
                    if not project_dir and conv.project_dir:
                        project_dir = conv.project_dir
                    elif project_dir and project_dir != conv.project_dir:
                        # 新的启动目录：跟随会话更新，保证 /resume 后目录一致
                        repo.update_conversation(db, conversation_id, project_dir=project_dir)
            except Exception as exc:
                logger.warning("读取会话工作目录失败：%s", exc)

        queue: Queue = Queue()
        bus = EventBus(queue)
        runtime: dict = {
            "conv_id": conversation_id,
            "started": time.perf_counter(),
            "final_text": "",
            "status": "ok",
            "error": None,
            "token_usage": None,
            # 请求级沙箱覆盖（CLI --sandbox / /sandbox）：空则跟随设置页
            "command_sandbox": command_sandbox,
            # 分阶段耗时打点（节点包装器与 agent 节点写入，finalize 落库）
            "timings": {},
        }
        state: AgentState = {
            "service": self,
            "bus": bus,
            "db": db,
            "runtime": runtime,
            "stop_event": stop_event,
            "question": question,
            "images": images,
            "use_web_search": use_web_search,
            "use_knowledge_base": use_knowledge_base,
            "conversation_id": conversation_id,
            "template_id": template_id,
            "system_prompt": system_prompt,
            "tool_mode": tool_mode,
            "plan_only": bool(plan_only),
            "resume_plan": list(resume_plan) if resume_plan else None,
            "project_dir": project_dir,
            "messages": [],
            "tools": [],
            "pending_tool_calls": [],
            "sources": [],
            "tool_trace": [],
            "counter": [0],
            "tool_calls_used": 0,
            "failure_count": 0,
            "forced_final": False,
            "last_call_warned": False,
            "plan_steps": [],
            "plan_map": [],
            "kb_documents": [],
            "memory_hits": [],
            "memory_summary": None,
            "vision_descriptions": [],
            "summary_text": None,
            "todos": [],
            "plan_done_count": 0,
            "force_continue": False,
            "plan_push_count": 0,
            "xml_retry_count": 0,
            "early_created": early_created,
            "dispatch_done": False,
            "task_mode": False,
            "subagent_results": [],
            "sub_task": {},
            "title_holder": [],
            "title_thread": None,
            "persist_start": 0,
        }

        def worker():
            # 本轮所有 LLM 调用的 token 用量聚合（线程级 callback）
            reset_usage()
            try:
                self.graph.invoke(
                    state,
                    config={
                        "recursion_limit": self.recursion_limit,
                        "configurable": {
                            "thread_id": f"conv:{conversation_id or uuid.uuid4().hex}"
                        },
                    },
                )
                queue.put(None)
            except Exception as exc:
                logger.exception("LangGraph Agent 执行失败")
                runtime["status"] = "error"
                runtime["error"] = str(exc)
                # 图步数上限（Recursion limit）友好兜底：README 承诺"超限也有
                # 最终回答"，裸报错体验差。工具轮多（含"伪成功"重试）时会触发，
                # 给出进度汇报式收尾而非只有 error 消息（checkpoint 已保存，
                # 回复"继续"可接着做）。
                if "Recursion limit" in str(exc) and not runtime.get("final_text"):
                    msg = (
                        "任务执行步骤过多，已触发图步数上限自动停止。"
                        "已完成的内容已保留；回复“继续”可以接着做。"
                    )
                    runtime["final_text"] = msg
                    if bus is not None:
                        bus.emit("token", msg)
                queue.put({"event": "error", "data": {"message": str(exc)}})
                try:
                    # 超限/异常兜底：保证有 done 事件与运行记录
                    _finalize_node(state)
                except Exception as inner:
                    logger.warning("兜底收尾失败：%s", inner)
                queue.put(None)

        threading.Thread(target=worker, daemon=True).start()

        while True:
            event = queue.get()
            if event is None:
                break
            yield event

        # done 之后：后台提取长期事实记忆（不阻塞流式输出）
        if (
            db is not None
            and runtime.get("conv_id") is not None
            and runtime.get("status") == "ok"
            and runtime.get("final_text")
        ):
            try:
                force_memory = self.context.has_memory_intent(question)
                auto_extract = (
                    len(runtime.get("final_text") or "")
                    >= int(getattr(settings, "memory_auto_extract_min_chars", 400) or 400)
                )
                # 短问答跳过记忆提取，减少一轮无必要 LLM 调用；显式"记住"始终提取
                if force_memory or auto_extract:
                    facts = self.context.extract_facts(
                        question,
                        runtime["final_text"],
                        force=force_memory,
                    )
                    if facts:
                        added = self.context.add_facts(
                            db, self.rag.embeddings, facts, runtime["conv_id"]
                        )
                        if added:
                            logger.info("新增 %d 条长期记忆", added)
                        try:
                            result = self.context.maybe_consolidate(db)
                            if result and (result["merged"] or result["archived"]):
                                logger.info("长期记忆自动整合：%s", result)
                            # 记忆变化后同步导出文件型项目记忆（AGENTS.md）
                            try:
                                from ..project_memory import export_project_memory

                                export_project_memory(self.settings, db)
                            except Exception as exc:
                                logger.warning("项目记忆导出失败：%s", exc)
                        except Exception as exc:
                            logger.warning("长期记忆自动整合失败：%s", exc)
            except Exception as exc:
                logger.warning("长期记忆提取失败：%s", exc)
