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


def resolve_kb_scope(settings, kb_scope: dict | None) -> tuple[list[str], str]:
    """把 {type: doc|book, value} 解析成相对路径列表与展示标签。

    未匹配到任何文件时返回 ([], "")，由 API 层拒绝请求（避免静默退回全库）。
    """
    if not kb_scope:
        return [], ""
    scope_type = str(kb_scope.get("type") or "doc").strip().lower()
    value = str(kb_scope.get("value") or "").strip()
    if not value and scope_type != "docs":
        return [], ""
    try:
        from ...rag.loader import list_data_files
        from ...rag.retriever import match_book_paths

        rel_paths = [
            p.relative_to(settings.data_dir).as_posix()
            for p in list_data_files(settings.data_dir)
        ]
    except Exception as exc:
        logger.warning("解析知识库范围失败：%s", exc)
        return [], ""
    if scope_type == "doc":
        matches = [r for r in rel_paths if r == value or r.endswith("/" + value)]
        if len(matches) != 1:
            return [], ""
        return matches, matches[0].rsplit("/", 1)[-1]
    if scope_type == "book":
        matched = match_book_paths([value], rel_paths)
        return (matched, value) if matched else ([], "")
    if scope_type == "docs":
        matched: list[str] = []
        for raw in kb_scope.get("values") or []:
            item = str(raw or "").strip()
            if not item:
                continue
            hits = [r for r in rel_paths if r == item or r.endswith("/" + item)]
            if len(hits) != 1:
                continue
            if hits[0] not in matched:
                matched.append(hits[0])
        if not matched:
            return [], ""
        names = [m.rsplit("/", 1)[-1] for m in matched[:3]]
        label = "、".join(names)
        if len(matched) > 3:
            label += f" 等 {len(matched)} 个文档"
        return matched, label
    return [], ""

# 看门狗自动续跑的会话集合：这条续跑不经过 API 的 _ACTIVE_CONVS 注册表，
# 但客户端轮询 /agent/pending 时需要知道"没有挂起但仍在跑"，否则会把
# 自动续跑的间隙误判成已结束。api/agent.py 的 is_conversation_active 会合并它。
_AUTO_RUNNING: set[int] = set()
_AUTO_LOCK = threading.Lock()


def is_auto_running(conversation_id: int | None) -> bool:
    if conversation_id is None:
        return False
    with _AUTO_LOCK:
        return conversation_id in _AUTO_RUNNING


class RunMixin(ServiceBaseMixin):
    def start_run(
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
        attachments: list[dict] | None = None,
        plan_only: bool = False,
        project_dir: str | None = None,
        command_sandbox: str | None = None,
        kb_scope: dict | None = None,
        quote: dict | None = None,
        *,
        event_sink,
    ) -> threading.Thread:
        """SSE 直连入口：事件经 event_sink 推送，不再由生成器中转。

        返回 graph worker 线程；调用方（API）直接消费 sink 队列即可，
        省掉 `for event in run(): queue.put(event)` 的桥接线程。
        """
        owned = False
        if db is None:
            try:
                from ...db.database import SessionLocal, db_ready

                if db_ready and SessionLocal is not None:
                    db = SessionLocal()
                    owned = True
            except Exception as exc:
                logger.warning("数据库会话不可用，降级为无记忆模式：%s", exc)
        try:
            return self._run_core(
                question,
                use_web_search,
                use_knowledge_base,
                conversation_id,
                template_id,
                system_prompt,
                db,
                tool_mode,
                stop_event,
                images,
                attachments,
                plan_only=plan_only,
                project_dir=project_dir,
                command_sandbox=command_sandbox,
                kb_scope=kb_scope,
                quote=quote,
                emit=event_sink,
                close_db=owned,
            )
        except Exception:
            if owned and db is not None:
                try:
                    db.close()
                except Exception:
                    pass
            raise


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
        attachments: list[dict] | None = None,
        plan_only: bool = False,
        project_dir: str | None = None,
        command_sandbox: str | None = None,
        kb_scope: dict | None = None,
        quote: dict | None = None,
    ):
        """CLI/测试入口：阻塞队列 + 生成器逐事件产出。"""
        queue: Queue = Queue()
        self._run_core(
            question,
            use_web_search,
            use_knowledge_base,
            conversation_id,
            template_id,
            system_prompt,
            db,
            tool_mode,
            stop_event,
            images,
            attachments,
            plan_only=plan_only,
            project_dir=project_dir,
            command_sandbox=command_sandbox,
            kb_scope=kb_scope,
            quote=quote,
            emit=queue.put,
        )
        while True:
            event = queue.get()
            if event is None:
                break
            yield event


    def _run_core(
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
        attachments: list[dict] | None = None,
        plan_only: bool = False,
        project_dir: str | None = None,
        command_sandbox: str | None = None,
        kb_scope: dict | None = None,
        quote: dict | None = None,
        *,
        emit,
        close_db: bool = False,
    ) -> threading.Thread:
        """执行 LangGraph 编排，事件通过 emit 推送；返回 graph worker 线程。"""
        settings = self.settings
        images = [img for img in (images or []) if img and img.strip()][
            : int(effective(settings, "vision_max_images") or 6)
        ]

        # 工具开关解析：tool_mode 优先，兼容旧前端布尔开关
        use_knowledge_base, use_web_search = self._resolve_tool_flags(
            tool_mode, use_web_search, use_knowledge_base
        )
        # 本轮知识库范围（单文档/整本书）：解析为相对路径，空 = 全库
        kb_scope_paths, kb_scope_label = resolve_kb_scope(settings, kb_scope)

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

        bus = EventBus(sink=emit)
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
            "attachments": attachments or [],
            "use_web_search": use_web_search,
            "use_knowledge_base": use_knowledge_base,
            "conversation_id": conversation_id,
            "template_id": template_id,
            "system_prompt": system_prompt,
            "tool_mode": tool_mode,
            "plan_only": bool(plan_only),
            "project_dir": project_dir,
            "kb_scope_paths": kb_scope_paths,
            "kb_scope_label": kb_scope_label,
            "quote": quote or {},
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
            "plan_submit_push_count": 0,
            "xml_retry_count": 0,
            # 每轮重置：checkpointer 按 conv:<id> 持久化 state，不重置的话
            # 一次用掉就整个会话再也享受不到空正文兜底（2026-09-24 实测）
            "empty_retry_count": 0,
            "empty_retry_pending": False,
            # 本轮的复验机会计数（prepare 也会复位；这里保证新会话/无检查点时初值一致）
            "verify_fail_count": 0,
            "early_created": early_created,
            "dispatch_rounds": 0,
            "task_mode": False,
            "subagent_results": [],
            "sub_task": {},
            "persist_start": 0,
            # D 块版本账本（prepare 每轮读取并回写；首轮为空）
            "dblock_versions": {},
        }
        # 延迟统计：本段起点只在请求级 Deps 里，不进检查点
        deps.elapsed_ms = int(runtime.get("elapsed_ms") or 0)
        deps.started = time.perf_counter()

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
                    self._emit_suspended(bus, result, conversation_id, deps)
                    return
            except Exception as exc:
                logger.exception("LangGraph Agent 执行失败")
                runtime["status"] = "error"
                runtime["error"] = str(exc)
                # 图步数上限（Recursion limit）友好兜底：README 承诺"超限也有
                # 最终回答"，裸报错体验差。工具轮多（含"伪成功"重试）时会触发，
                # 给出进度汇报式收尾而非只有 error 消息（checkpoint 已保存，
                # 回复"继续"可以接着做）。
                if "Recursion limit" in str(exc) and not runtime.get("final_text"):
                    msg = (
                        "任务执行步骤过多，已触发图步数上限自动停止。"
                        "已完成的内容已保留；回复“继续”可以接着做。"
                    )
                    runtime["final_text"] = msg
                    bus.emit("token", msg)
                bus.emit("error", {"message": str(exc)})
                try:
                    # 超限/异常兜底：保证有 done 事件与运行记录（图外直调，给 runtime 替身）
                    _finalize_node(state, runtime_of(deps))
                except Exception as inner:
                    logger.warning("兜底收尾失败：%s", inner)
            finally:
                # 先启动后台记忆提取，再发哨兵：消费端看到 None 即可收流
                self._spawn_memory_postprocess(runtime, question)
                emit(None)
                if close_db and db is not None:
                    try:
                        db.close()
                    except Exception:
                        pass

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread


    def _spawn_memory_postprocess(self, runtime: dict, question: str) -> None:
        """done 之后：长期事实记忆提取放到独立后台线程 + 独立短会话。

        生成器/SSE 流随即结束，API 随之释放会话锁，用户马上追问不会撞 409。
        """
        conv_id = runtime.get("conv_id")
        final_text = runtime.get("final_text") or ""
        if not (
            conv_id is not None
            and runtime.get("status") == "ok"
            and final_text
        ):
            return

        def _postprocess_memory() -> None:
            bg_db = None
            try:
                from ...db.database import SessionLocal, db_ready

                if not db_ready or SessionLocal is None:
                    return
                bg_db = SessionLocal()
                force_memory = self.context.has_memory_intent(question)
                auto_extract = (
                    len(final_text)
                    >= int(
                        effective(self.settings, "memory_auto_extract_min_chars")
                        or 400
                    )
                )
                # 短问答跳过记忆提取，减少一轮无必要 LLM 调用；显式"记住"始终提取
                if not (force_memory or auto_extract):
                    return
                self.rag.acquire_llm()
                try:
                    facts = self.context.extract_facts(
                        question, final_text, force=force_memory
                    )
                finally:
                    self.rag.release_llm()
                if not facts:
                    return
                # 后台线程也要遵守 GPU 串行契约（BGE encode 不允许并发）
                self.rag.acquire_gpu()
                try:
                    added = self.context.add_facts(
                        bg_db, self.rag.embeddings, facts, conv_id
                    )
                finally:
                    self.rag.release_gpu()
                if not added:
                    return
                logger.info("新增 %d 条长期记忆", added)
                try:
                    result = self.context.maybe_consolidate(bg_db)
                    if result and (result["merged"] or result["archived"]):
                        logger.info("长期记忆自动整合：%s", result)
                    # 记忆变化后同步导出文件型项目记忆（AGENTS.md）
                    try:
                        from ...project_memory import export_project_memory

                        export_project_memory(self.settings, bg_db)
                    except Exception as exc:
                        logger.warning("项目记忆导出失败：%s", exc)
                except Exception as exc:
                    logger.warning("长期记忆自动整合失败：%s", exc)
            except Exception as exc:
                logger.warning("长期记忆提取失败：%s", exc)
            finally:
                if bg_db is not None:
                    try:
                        bg_db.close()
                    except Exception:
                        pass

        threading.Thread(
            target=_postprocess_memory,
            daemon=True,
            name=f"memory-{conv_id}",
        ).start()

    def _emit_suspended(
        self,
        bus: EventBus,
        result: dict,
        conversation_id: int | None,
        deps: Deps | None = None,
    ) -> None:
        """图在 interrupt() 处挂起：发 run_suspended，并快照本段用量/耗时。

        用量与耗时快照只放内存注册表（不写检查点）：恢复线程是新的
        thread-local，由 resume 端 seed 后与恢复段自然合并；后端重启则丢快照
        （少计一段，但不会产生负数/异常巨值）。
        """
        if deps is not None and conversation_id is not None:
            started = float(getattr(deps, "started", 0.0) or 0.0)
            segment = (
                max(0, round((time.perf_counter() - started) * 1000))
                if started
                else 0
            )
            self._suspended_elapsed[conversation_id] = (
                int(getattr(deps, "elapsed_ms", 0) or 0) + segment
            )
        interrupts = (result or {}).get("__interrupt__") or []
        payload = getattr(interrupts[0], "value", None) if interrupts else None
        payload = payload if isinstance(payload, dict) else {}
        ops = payload.get("ops") or []
        plan = payload.get("plan") or None
        questions = payload.get("questions") or []
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
        elif questions:
            # 向用户提问：永不超时，等用户回答/跳过
            count = sum(len(q.get("questions") or []) for q in questions)
            bus.emit(
                "status",
                {"phase": "question", "text": f"等待回答：{count} 个问题"},
            )
        bus.emit(
            "run_suspended",
            {
                "conversation_id": conversation_id,
                "ops": ops,
                "plan": plan,
                "questions": questions,
                # 批次戳：客户端轮询 /agent/pending 时据此区分同批/新批
                "created_at": payload.get("created_at"),
            },
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
            logger.info(
                "审批超时自动拒绝并续跑：conv=%s ops=%d timeout=%ss",
                conversation_id,
                len(decisions),
                timeout,
            )
            db = None
            with _AUTO_LOCK:
                _AUTO_RUNNING.add(conversation_id)
            try:
                from ...db.database import SessionLocal, db_ready

                if db_ready and SessionLocal is not None:
                    db = SessionLocal()
                for _ in self.resume(conversation_id, decisions, db=db):
                    pass
            except Exception as exc:
                logger.warning("审批超时兜底续跑失败：%s", exc)
            finally:
                with _AUTO_LOCK:
                    _AUTO_RUNNING.discard(conversation_id)
                if db is not None:
                    db.close()

        timer = threading.Timer(int(timeout), _fire)
        timer.daemon = True
        timer.start()
