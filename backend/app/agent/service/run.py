"""一次问答的运行编排：LangGraph invoke、结束/挂起收尾、审批超时看门狗。

从 LangGraphAgentService 拆出（2026-10-09）：只做移动，未改逻辑。"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from queue import Queue
from sqlalchemy.orm import Session
from ...db import repository as repo
from ...runtime_config import effective, effective_or
from ...tracing import get_aux_usage_collector, reset_usage, usage_summary
from ..nodes.finalize import _finalize_node
from ..state import AgentState, Deps, EventBus, runtime_of
from .base import ServiceBaseMixin


logger = logging.getLogger(__name__)


class RunMixin(ServiceBaseMixin):
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
        project_dir: str | None = None,
        command_sandbox: str | None = None,
    ):
        """执行 LangGraph 编排，逐事件产出（session/plan/vision/.../done）。"""
        settings = self.settings
        images = [img for img in (images or []) if img and img.strip()][
            : int(effective(settings, "vision_max_images") or 6)
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
        deps = Deps(service=self, bus=bus, db=db, stop_event=stop_event)
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
            # D 块版本账本（prepare 每轮读取并回写；首轮为空）
            "dblock_versions": {},
        }

        def worker():
            # 本轮所有 LLM 调用的 token 用量聚合（线程级 callback）
            reset_usage()
            try:
                result = self.graph.invoke(
                    state,
                    config={
                        "recursion_limit": self._recursion_limit(),
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
                    >= int(effective(settings, "memory_auto_extract_min_chars") or 400)
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
                                from ...project_memory import export_project_memory

                                export_project_memory(self.settings, db)
                            except Exception as exc:
                                logger.warning("项目记忆导出失败：%s", exc)
                        except Exception as exc:
                            logger.warning("长期记忆自动整合失败：%s", exc)
            except Exception as exc:
                logger.warning("长期记忆提取失败：%s", exc)

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
            timeout = int(
                effective_or(self.settings, "permission_timeout", 0) or 0
            )
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
                from ...db.database import SessionLocal, db_ready

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
