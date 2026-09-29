"""LangGraphAgentService：LangGraph 编排的服务入口（图构建 / 事件流 / 运行记录）。

2026-08-27 模块化重构：图定义、节点实现、工具辅助已拆分到
graph.py / nodes/ / utils.py / state.py，本文件仅保留服务类。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from queue import Queue

from langgraph.types import Command
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import repository as repo
from ..rag.service import RAGService
from ..runtime_config import effective
from ..tracing import (
    get_aux_usage_collector,
    reset_usage,
    seed_usage,
    usage_summary,
)
from .agent import AgentService
from .graph import build_agent_graph
from .nodes.finalize import _finalize_node
from .state import AgentState, Deps, EventBus, runtime_of

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
        # conv_id -> (main, aux)：挂起前一段的用量快照，恢复线程 seed 后合并
        self._suspended_usage: dict = {}

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
        """设置变更后重置懒加载缓存；图结构不变，无需重建。

        不重置 MCP 管理器：连接由 MCPManager.configure() 按配置签名自行
        增删/重建，重建设置页保存会把所有 MCP 连接掐断重连（连接失败即
        工具数组变化 → 该模型全部会话的前缀缓存作废）。
        """
        super().refresh()

    # ---------------- 扩展工具（P0：MCP / 受控执行）----------------

    def _mcp_cache_dir(self):
        """MCP 工具 schema 缓存目录（连接失败时靠它保持工具定义字节稳定）。"""
        return self.settings.data_dir / "mcp_schema"

    def mcp_tools(self, db: Session | None) -> list:
        """加载启用中的 MCP 服务器工具（连接失败自动降级跳过）。"""
        if not effective(self.settings, "mcp_enabled", True):
            return []
        if self._mcp is None:
            from ..mcp_manager import MCPManager

            self._mcp = MCPManager(cache_dir=self._mcp_cache_dir())
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
        # 请求级活对象走 Runtime context（不进检查点）；state 只留可序列化数据
        # approval_mode：有原生检查点走 interrupt 挂起，否则回落旧的阻塞式收集
        deps = Deps(
            service=self,
            bus=bus,
            db=db,
            stop_event=stop_event,
            approval_mode="interrupt" if self.checkpoint_saver is not None else "blocking",
        )
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
            "runtime": runtime,
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
            "pending_tool_calls": [],
            "pending_subtasks": [],
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
            # 每轮重置：checkpointer 按 conv:<id> 持久化 state，不重置的话
            # 一次用掉就整个会话再也享受不到空正文兜底（2026-09-24 实测）
            "empty_retry_count": 0,
            "empty_retry_pending": False,
            "early_created": early_created,
            "dispatch_rounds": 0,
            "task_mode": False,
            "subagent_results": [],
            "sub_task": {},
            "persist_start": 0,
        }

        def worker():
            # 本轮所有 LLM 调用的 token 用量聚合（线程级 callback）
            reset_usage()
            try:
                result = self.graph.invoke(
                    state,
                    config={
                        "recursion_limit": self.recursion_limit,
                        "configurable": {
                            "thread_id": f"conv:{conversation_id or uuid.uuid4().hex}"
                        },
                    },
                    context=deps,
                )
                if isinstance(result, dict) and result.get("__interrupt__"):
                    # 敏感操作挂起：本轮流到此为止（客户端收 run_suspended 后走 /agent/resume）
                    self._emit_suspended(bus, result, conversation_id)
                    queue.put(None)
                    return
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
                    # 超限/异常兜底：保证有 done 事件与运行记录（图外直调，给 runtime 替身）
                    _finalize_node(state, runtime_of(deps))
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

    # ---------------- 工具面构建（prepare / 挂起恢复共用）----------------

    def build_tool_list(
        self,
        db: Session | None,
        *,
        use_web_search: bool,
        use_knowledge_base: bool,
        conv_id: int | None,
        project_dir: str | None,
        command_sandbox: str | None,
    ) -> list:
        """按开关组装本轮工具集。

        挂起恢复不重跑 prepare，必须用同一套构建逻辑重建 Deps.tools，
        否则 tools 节点里所有工具都会查不到（"未知工具"）。
        顺序即 bind_tools 的 tools 数组顺序，参与前缀缓存键，勿随意调整。
        """
        settings = self.settings
        from .tools import (
            build_tools,
            make_enter_plan_mode_tool,
            make_exit_plan_mode_tool,
        )

        skills_on = effective(settings, "skills_enabled", True)
        skill_enabled_ids = None
        if db is not None:
            try:
                from ..skills import load_prefs

                skill_enabled_ids = set((load_prefs(db) or {}).get("enabled") or [])
            except Exception as exc:
                logger.warning("技能偏好加载失败：%s", exc)
                skill_enabled_ids = None
        tools = build_tools(
            self.rag,
            use_web_search,
            use_knowledge_base,
            effective(settings, "web_search_provider"),
            settings.tavily_api_key,
            effective(settings, "web_search_max_results"),
            crag_enabled=settings.crag_fallback_enabled,
            crag_min_score=settings.crag_min_score,
            vision=self.vision,
            skills_enabled=skills_on,
            skill_enabled_ids=skill_enabled_ids,
            searxng_base_url=effective(settings, "searxng_base_url") or "",
            searxng_engines=effective(settings, "searxng_engines") or "",
        )
        # 扩展工具：MCP + 受控执行（文件/命令，敏感操作走人工确认）
        tools.extend(self.mcp_tools(db))
        tools.extend(
            self.extra_tools(project_dir=project_dir, sandbox=command_sandbox)
        )
        # 用户自写工具（动态目录）：每次重建自动加载（新工具下一个请求即生效）
        try:
            from ..user_tool_loader import load_user_tools

            tools.extend(load_user_tools())
        except Exception as exc:
            logger.warning("用户自写工具加载失败：%s", exc)
        if db is not None and conv_id is not None:
            try:
                from ..todos import make_todo_tool

                tools.append(make_todo_tool(db, conv_id))
            except Exception as exc:
                logger.warning("任务清单工具加载失败：%s", exc)
        # 声明式派发工具：让主代理（而非 planner + 规则层）决定派什么子任务。
        # 必须**无条件**注册——bind_tools 的 tools 数组是 DeepSeek 前缀缓存键的
        # 一部分，会话内变化会整链断缓存（CLAUDE.md 不变量）。
        if effective(settings, "agent_subagents_enabled") is not False:
            try:
                from .nodes.subagent import make_dispatch_tool

                tools.append(make_dispatch_tool(settings))
            except Exception as exc:
                logger.warning("派发工具加载失败：%s", exc)
        # 计划模式控制工具：设置页开关（runtime_config）关闭时不注册——模型物理上
        # 无法进入，是硬否决而不是靠提示词自觉。
        if effective(settings, "plan_mode_allowed", True) is not False:
            try:
                tools.append(make_enter_plan_mode_tool())
                tools.append(make_exit_plan_mode_tool())
            except Exception as exc:
                logger.warning("计划模式工具加载失败：%s", exc)
        return tools

    # ---------------- 挂起 / 恢复（原生 interrupt）----------------

    def _thread_config(self, conversation_id: int) -> dict:
        return {
            "recursion_limit": self.recursion_limit,
            "configurable": {"thread_id": f"conv:{conversation_id}"},
        }

    def _emit_suspended(
        self, bus: EventBus, result: dict, conversation_id: int | None
    ) -> None:
        """图在 interrupt() 处挂起：发 run_suspended，并快照本段用量。

        用量快照只放内存注册表（不写检查点）：恢复线程是新的 thread-local，
        由 resume 端 seed 后与恢复段自然合并；后端重启则丢快照（少计一段）。
        """
        interrupts = (result or {}).get("__interrupt__") or []
        payload = getattr(interrupts[0], "value", None) if interrupts else None
        payload = payload if isinstance(payload, dict) else {}
        ops = payload.get("ops") or []
        plan = payload.get("plan") or None
        if conversation_id is not None:
            self._suspended_usage[conversation_id] = (
                usage_summary(),
                get_aux_usage_collector().summary(),
            )
        if ops:
            text = (
                f"等待确认：{ops[0].get('summary')}"
                if len(ops) == 1
                else f"等待确认：{len(ops)} 项操作"
            )
            bus.emit("status", {"phase": "permission", "text": text})
            timeout = int(effective(self.settings, "permission_timeout", 0) or 0)
            if timeout > 0 and conversation_id is not None:
                self._arm_permission_watchdog(
                    conversation_id, timeout, payload.get("created_at")
                )
        elif plan:
            # 计划确认：无超时（与旧 awaiting_approval 行为一致），等用户决定
            bus.emit("status", {"phase": "plan", "text": "等待确认：执行计划"})
        bus.emit(
            "run_suspended",
            {"conversation_id": conversation_id, "ops": ops, "plan": plan},
        )

    def _arm_permission_watchdog(
        self, conversation_id: int, timeout: int, expected_created_at
    ) -> None:
        """permission_timeout>0：到点仍挂起则以「全部拒绝」自动续跑。

        定时器绑定挂起批次（created_at 戳）：过期定时器发现当前挂起不是自己
        那一批时直接放弃，避免误伤后续轮次的审批。
        """

        def _fire():
            payload = self.pending_approval_payload(conversation_id)
            if not payload or payload.get("created_at") != expected_created_at:
                return
            decisions = {
                str(op.get("call_id")): {
                    "approve": False,
                    "reason": "等待确认超时，已自动取消",
                }
                for op in (payload.get("ops") or [])
                if op.get("call_id")
            }
            db = None
            try:
                from ..db.database import SessionLocal, db_ready

                if db_ready and SessionLocal is not None:
                    db = SessionLocal()
                for _ in self.resume(conversation_id, decisions, db=db):
                    pass
            except Exception as exc:
                logger.warning("审批超时兜底续跑失败：%s", exc)
            finally:
                if db is not None:
                    db.close()

        timer = threading.Timer(int(timeout), _fire)
        timer.daemon = True
        timer.start()

    def pending_approval_payload(self, conversation_id: int) -> dict | None:
        """读取挂起中的 interrupt 载荷（无挂起/读取失败时返回 None）。

        判据用 `tasks[].interrupts`，不要用 `get_state().next`——实测多
        interrupt 场景下恢复一段后 next 会是空元组，但任务仍挂起。
        """
        if self.checkpoint_saver is None:
            return None
        try:
            st = self.graph.get_state(self._thread_config(conversation_id))
            for task in st.tasks or ():
                for item in getattr(task, "interrupts", None) or ():
                    value = getattr(item, "value", None)
                    if isinstance(value, dict):
                        return value
        except Exception as exc:
            logger.warning("读取挂起审批失败：%s", exc)
        return None

    def has_pending_approval(self, conversation_id: int | None) -> bool:
        """该会话是否有挂起中的审批（挂起期间拒绝新消息，防状态被覆盖）。"""
        if conversation_id is None or self.checkpoint_saver is None:
            return False
        try:
            st = self.graph.get_state(self._thread_config(conversation_id))
            return any(
                getattr(task, "interrupts", None) for task in (st.tasks or ())
            )
        except Exception:
            return False

    def resume(
        self,
        conversation_id: int,
        decisions: dict,
        stopped: bool = False,
        plan: dict | None = None,
        db: Session | None = None,
        stop_event: threading.Event | None = None,
    ):
        """恢复挂起的运行：Command(resume=...) 续跑同一 thread，事件协议同 _run。

        stopped=True 表示用户在挂起期间点了停止：预置 stop_event 并以全拒决定
        续跑，收尾语义与旧阻塞式的"停止"路径一致。
        plan：计划确认决定（{approved, steps?, reason?}），随同一次 resume 提交。
        """
        queue: Queue = Queue()
        bus = EventBus(queue)
        if stop_event is None:
            stop_event = threading.Event()
        if stopped:
            stop_event.set()
        # db 缺省（SSE 端点走这里）：自建会话，语义与 AgentService.run 一致
        db_owned = None
        if db is None:
            try:
                from ..db.database import SessionLocal, db_ready

                if db_ready and SessionLocal is not None:
                    db_owned = SessionLocal()
                    db = db_owned
            except Exception as exc:
                logger.warning("恢复运行：数据库不可用，降级为无记忆模式：%s", exc)
        deps = Deps(
            service=self,
            bus=bus,
            db=db,
            stop_event=stop_event,
            approval_mode="interrupt" if self.checkpoint_saver is not None else "blocking",
        )
        config = self._thread_config(conversation_id)
        try:
            values = self.graph.get_state(config).values or {}
        except Exception:
            values = {}
        rt = values.get("runtime") or {}
        try:
            deps.tools = self.build_tool_list(
                db,
                use_web_search=bool(values.get("use_web_search")),
                use_knowledge_base=bool(values.get("use_knowledge_base")),
                conv_id=conversation_id,
                project_dir=values.get("project_dir"),
                command_sandbox=(rt or {}).get("command_sandbox"),
            )
        except Exception as exc:
            logger.warning("恢复时重建工具列表失败：%s", exc)

        bus.emit("session", {"conversation_id": conversation_id})

        def worker():
            seed = self._suspended_usage.pop(conversation_id, None)
            reset_usage()
            if seed:
                seed_usage(seed[0], seed[1])
            try:
                result = self.graph.invoke(
                    Command(
                        resume={
                            "stopped": bool(stopped),
                            "decisions": decisions or {},
                            "plan": plan,
                        }
                    ),
                    config=config,
                    context=deps,
                )
                if isinstance(result, dict) and result.get("__interrupt__"):
                    # 恢复后新一轮敏感操作再次挂起：同样发 run_suspended
                    self._emit_suspended(bus, result, conversation_id)
                    queue.put(None)
                    return
                queue.put(None)
            except Exception as exc:
                logger.exception("恢复运行失败")
                if isinstance(rt, dict):
                    rt["status"] = "error"
                    rt["error"] = str(exc)
                queue.put({"event": "error", "data": {"message": str(exc)}})
                try:
                    _finalize_node(values, runtime_of(deps))
                except Exception as inner:
                    logger.warning("兜底收尾失败：%s", inner)
                queue.put(None)

        threading.Thread(target=worker, daemon=True).start()

        try:
            while True:
                event = queue.get()
                if event is None:
                    break
                yield event
        finally:
            if db_owned is not None:
                db_owned.close()
