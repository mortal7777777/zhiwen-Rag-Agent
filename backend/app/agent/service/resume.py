"""挂起/中断后的续跑：审批恢复、崩溃续跑、丢弃运行，以及流式骨架。

从 LangGraphAgentService 拆出（2026-10-09）：只做移动，未改逻辑。"""

from __future__ import annotations

import logging
import threading
from queue import Queue
from langgraph.types import Command
from sqlalchemy.orm import Session
from ...tracing import reset_usage, seed_usage
from ..state import Deps, EventBus, runtime_of
from ..nodes.finalize import _finalize_node
from .base import ServiceBaseMixin


logger = logging.getLogger(__name__)


class ResumeMixin(ServiceBaseMixin):
    def pending_state(self, conversation_id: int | None) -> dict:
        """线程未完成状态：{pending, interrupted, ops, plan, _payload}。

        - pending：有 interrupt 等用户决定（审批/计划确认），走 /agent/resume；
        - interrupted：有未完成任务但没有挂起（进程被杀/异常退出留下的半成品），
          走 /agent/continue（invoke(None) 续跑）。
        判据用 `tasks[].interrupts`，不要用 `get_state().next`——实测多 interrupt
        场景下恢复一段后 next 会是空元组，但任务仍挂起。
        """
        result = {
            "pending": False,
            "interrupted": False,
            "ops": [],
            "plan": None,
            "_payload": None,
        }
        if conversation_id is None or self.checkpoint_saver is None:
            return result
        try:
            st = self.graph.get_state(self._thread_config(conversation_id))
        except Exception as exc:
            logger.warning("读取线程状态失败：%s", exc)
            return result
        pending_payload = None
        has_tasks = False
        for task in st.tasks or ():
            has_tasks = True
            if pending_payload is None:
                for item in getattr(task, "interrupts", None) or ():
                    value = getattr(item, "value", None)
                    if isinstance(value, dict):
                        pending_payload = value
                        break
        if pending_payload is not None:
            result["pending"] = True
            result["ops"] = pending_payload.get("ops") or []
            result["plan"] = pending_payload.get("plan") or None
            result["_payload"] = pending_payload
        elif has_tasks:
            result["interrupted"] = True
        return result

    def pending_approval_payload(self, conversation_id: int) -> dict | None:
        """挂起中的 interrupt 载荷（无挂起/读取失败时返回 None；watchdog 用）。"""
        return self.pending_state(conversation_id).get("_payload")

    def has_pending_approval(self, conversation_id: int | None) -> bool:
        """该会话是否有挂起中的审批（挂起期间拒绝新消息，防状态被覆盖）。"""
        return bool(self.pending_state(conversation_id)["pending"])

    def has_unfinished_run(self, conversation_id: int | None) -> bool:
        """挂起中或中断未跑完（两种都拒绝新消息：新输入会覆盖未完成的一轮）。"""
        st = self.pending_state(conversation_id)
        return bool(st["pending"] or st["interrupted"])

    def abandon_run(self, conversation_id: int) -> bool:
        """丢弃未完成的运行：删除该会话的原生检查点线程，解除新消息闸门。"""
        if self.checkpoint_saver is None:
            return False
        try:
            self.checkpoint_saver.delete_thread(f"conv:{conversation_id}")
            return True
        except Exception as exc:
            logger.warning("删除会话检查点线程失败：%s", exc)
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
        if stop_event is None:
            stop_event = threading.Event()
        if stopped:
            stop_event.set()
        yield from self._continue_stream(
            conversation_id,
            Command(
                resume={
                    "stopped": bool(stopped),
                    "decisions": decisions or {},
                    "plan": plan,
                }
            ),
            db=db,
            stop_event=stop_event,
        )

    def continue_run(
        self,
        conversation_id: int,
        db: Session | None = None,
        stop_event: threading.Event | None = None,
    ):
        """续跑被中断的运行（进程被杀/异常退出留下的 pending 任务）：invoke(None)。

        与 resume 的区别：resume 回答挂起中的 interrupt（提交决定）；continue
        让框架把没跑完的节点重跑完。只重跑未完成节点、不重跑 prepare，前缀
        缓存不断。被中断节点从头重跑，副作用可能重复一次（窗口=进程恰好死在
        节点执行中；已提交的决定不会丢——RESUME 通道随检查点持久化）。
        """
        yield from self._continue_stream(
            conversation_id,
            None,
            db=db,
            stop_event=stop_event if stop_event is not None else threading.Event(),
        )

    def _continue_stream(
        self,
        conversation_id: int,
        graph_input,
        db: Session | None,
        stop_event: threading.Event,
    ):
        """resume / continue 共用的续跑骨架：重建 Deps、invoke、产出事件流。"""
        queue: Queue = Queue()
        bus = EventBus(queue)
        # db 缺省（SSE 端点走这里）：自建会话，语义与 AgentService.run 一致
        db_owned = None
        if db is None:
            try:
                from ...db.database import SessionLocal, db_ready

                if db_ready and SessionLocal is not None:
                    db_owned = SessionLocal()
                    db = db_owned
            except Exception as exc:
                logger.warning("续跑：数据库不可用，降级为无记忆模式：%s", exc)
        deps = Deps(service=self, bus=bus, db=db, stop_event=stop_event)
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
            logger.warning("续跑时重建工具列表失败：%s", exc)

        bus.emit("session", {"conversation_id": conversation_id})

        def worker():
            seed = self._suspended_usage.pop(conversation_id, None)
            reset_usage()
            if seed:
                seed_usage(seed[0], seed[1])
            try:
                result = self.graph.invoke(graph_input, config=config, context=deps)
                if isinstance(result, dict) and result.get("__interrupt__"):
                    # 续跑后再次挂起（新一轮敏感操作/计划）：同样发 run_suspended
                    self._emit_suspended(bus, result, conversation_id)
                    queue.put(None)
                    return
                queue.put(None)
            except Exception as exc:
                logger.exception("续跑运行失败")
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
