"""Agent 接口：普通 JSON 模式 + SSE 流式模式。"""

from __future__ import annotations

import json
import logging
import threading
import asyncio
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..agent.agent import AgentService
from ..agent.branch_control import RunCancellation
from ..agent.service.run import is_auto_running, resolve_kb_scope
from ..attachments import (
    AttachmentError,
    delete_attachment,
    load_attachments,
    save_upload,
)
from ..config import get_settings
from ..db import get_db
from ..db import repository as repo
from ..schemas import (
    AgentAttachmentUploadResponse,
    AgentChatRequest,
    AgentChatResponse,
    AgentResumeRequest,
)
from .deps import get_agent_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["agent"])

# 正在运行的流式请求注册表：run_id -> stop_event。
# 2026-11-19 方案 A：客户端断开（刷新/关页）不再自动中止运行——run 后台跑完
# 落库，「停止」必须走 /agent/cancel/{run_id} 显式置信号（Web 前端与 CLI 同款）。
# 生命周期：登记于 SSE 生成器启动，释放于事件流的终止信号（done/run_suspended/
# None 哨兵，见 _enqueue），而不是 SSE 连接关闭。
_ACTIVE_RUNS: dict[str, threading.Event] = {}
# 会话 -> run_id：并发闸门（同会话同时只允许一条流，防检查点状态互相覆盖）
_ACTIVE_CONVS: dict[int, str] = {}
_RUNS_LOCK = threading.Lock()


def try_register_active_run(
    stop_event: threading.Event, conversation_id: int | None = None
) -> str | None:
    """原子地「检查 + 登记」一次运行；会话已有运行中的流时返回 None。

    端点层的 `_reject_if_busy` 只是提前给出友好的 409；真正防并发靠这里
    （检查与登记在同一把锁内，不存在两个请求都通过的 TOCTOU 窗口）。
    """
    run_id = uuid.uuid4().hex
    with _RUNS_LOCK:
        if conversation_id is not None and conversation_id in _ACTIVE_CONVS:
            return None
        _ACTIVE_RUNS[run_id] = stop_event
        if conversation_id is not None:
            _ACTIVE_CONVS[conversation_id] = run_id
    return run_id


def unregister_active_run(run_id: str) -> None:
    with _RUNS_LOCK:
        _ACTIVE_RUNS.pop(run_id, None)
        stale = [cid for cid, rid in _ACTIVE_CONVS.items() if rid == run_id]
        for cid in stale:
            _ACTIVE_CONVS.pop(cid, None)


def is_conversation_active(conversation_id: int | None) -> bool:
    """该会话是否有正在进行的运行（含看门狗触发的无人值守续跑）。"""
    if conversation_id is None:
        return False
    with _RUNS_LOCK:
        active = conversation_id in _ACTIVE_CONVS
    return active or is_auto_running(conversation_id)


def request_cancel(run_id: str) -> tuple[bool, str]:
    """置运行停止信号；返回 (是否成功, 失败原因)。"""
    with _RUNS_LOCK:
        stop_event = _ACTIVE_RUNS.get(run_id)
    if stop_event is None:
        return False, "run not found"
    stop_event.set()
    return True, ""


def _sse(payload: dict) -> str:
    """SSE 事件帧：data: {json}\n\n"""
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _code_error(status: int, code: str, message: str) -> HTTPException:
    """带结构化错误码的 HTTP 错误：detail 仍是人读文案（CLI/弹窗直接显示），
    响应头 X-Zhiwen-Code 供前端做控制流（恢复挂起卡/中断条），不再匹配中文文案。

    约定码（仅会话闸门类）：
    - pending_approval：有待确认的审批/计划/提问
    - run_interrupted：上一轮执行被中断，可继续或丢弃
    - conversation_busy：该会话已有正在进行的回复
    - no_pending / no_interrupted_run：请求的挂起/中断状态不存在
    """
    return HTTPException(
        status_code=status,
        detail=message,
        headers={"X-Zhiwen-Code": code},
    )


@router.post("/agent/attachments", response_model=AgentAttachmentUploadResponse)
def upload_agent_attachments(
    files: list[UploadFile] = File(..., description="对话附件（文档/文本/代码）"),
) -> dict:
    """上传对话附件并提取文本；返回 id，发送消息时随 attachment_ids 携带。

    附件只用于本轮/当前会话的上下文注入，不进入知识库索引；临时文件按
    ATTACHMENT_RETENTION_HOURS 自动清理。
    """
    settings = get_settings()
    max_files = max(1, int(getattr(settings, "attachment_max_files", 5) or 5))
    if not files:
        raise HTTPException(status_code=400, detail="没有上传文件")
    if len(files) > max_files:
        raise HTTPException(
            status_code=400, detail=f"单轮最多上传 {max_files} 个附件"
        )
    attachments = []
    for upload in files:
        try:
            attachments.append(save_upload(settings, upload))
        except AttachmentError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"attachments": attachments}


@router.delete("/agent/attachments/{attachment_id}")
def delete_agent_attachment(attachment_id: str) -> dict:
    """删除尚未发送的附件（前端移除 chip 时调用）。"""
    if not delete_attachment(get_settings(), attachment_id):
        raise HTTPException(status_code=404, detail="附件不存在或已过期")
    return {"ok": True}


def _kb_scope_payload(request: AgentChatRequest) -> dict | None:
    """校验并返回本轮知识库范围；未匹配到文档时 400（不静默退回全库）。"""
    if not request.kb_scope:
        return None
    payload = request.kb_scope.model_dump()
    paths, _label = resolve_kb_scope(get_settings(), payload)
    if not paths:
        raise HTTPException(
            status_code=400,
            detail="未找到匹配的知识库文档/书籍，请检查限定范围",
        )
    return payload


@router.post("/agent/chat", response_model=AgentChatResponse)
def agent_chat(
    request: AgentChatRequest,
    service: AgentService = Depends(get_agent_service),
) -> dict:
    """非流式 Agent 问答：工具调用（按需）+ 记忆 + 最终回答。"""
    kb_scope = _kb_scope_payload(request)
    _reject_if_busy(request.conversation_id, service)
    # 非流式同样要占住会话：否则它会与流式请求并发写同一个检查点线程
    stop_event = RunCancellation()
    run_id = try_register_active_run(stop_event, request.conversation_id)
    if run_id is None:
        raise _code_error(409, "conversation_busy", "该会话已有正在进行的回复")
    try:
        return service.run_json(
            question=request.question,
            use_web_search=request.use_web_search,
            use_knowledge_base=request.use_knowledge_base,
            conversation_id=request.conversation_id,
            template_id=request.template_id,
            system_prompt=request.system_prompt,
            tool_mode=request.tool_mode,
            stop_event=stop_event,
            images=request.images,
            attachments=load_attachments(get_settings(), request.attachment_ids),
            plan_only=request.plan_only,
            project_dir=request.project_dir,
            command_sandbox=request.command_sandbox,
            kb_scope=kb_scope,
            quote=request.quote.model_dump() if request.quote else None,
        )
    except Exception as exc:
        logger.exception("Agent 问答失败")
        raise HTTPException(status_code=500, detail=f"问答失败：{exc}")
    finally:
        unregister_active_run(run_id)


def _reject_if_busy(conversation_id: int | None, service: AgentService) -> None:
    """挂起中/中断未跑完/已有运行中的会话：拒绝新消息（防检查点状态被覆盖）。"""
    state_fn = getattr(service, "pending_state", None)
    if callable(state_fn):
        st = state_fn(conversation_id) or {}
        if st.get("pending"):
            raise _code_error(
                409,
                "pending_approval",
                "该会话有待确认的操作，请先批准或拒绝后再发送新消息",
            )
        if st.get("interrupted"):
            raise _code_error(
                409,
                "run_interrupted",
                "该会话上一轮执行被中断，请先点「继续执行」或丢弃该轮后再发送新消息",
            )
    else:
        has_pending = getattr(service, "has_pending_approval", None)
        if callable(has_pending) and has_pending(conversation_id):
            raise _code_error(
                409,
                "pending_approval",
                "该会话有待确认的操作，请先批准或拒绝后再发送新消息",
            )
    if is_conversation_active(conversation_id):
        raise _code_error(409, "conversation_busy", "该会话已有正在进行的回复")


def _service_source(service, kind: str, **kwargs):
    """构造事件源：优先走 start_*（sink 直连），否则回退生成器桥接。

    真实服务走 `start_run/start_resume/start_continue`：服务内部 graph worker
    直接向 sink 投递事件，API 不再需要 `for event in run(): queue.put()` 的
    桥接线程。测试/旧服务只有生成器，自动回退到桥接线程。
    """
    gen_name = "continue_run" if kind == "continue" else kind

    def source(emit, stop_event) -> None:
        starter = getattr(service, f"start_{kind}", None)
        if callable(starter):
            starter(**kwargs, stop_event=stop_event, event_sink=emit)
            return

        def bridge():
            try:
                for event in getattr(service, gen_name)(
                    **kwargs, stop_event=stop_event
                ):
                    emit(event)
            except Exception as exc:
                logger.exception("Agent 流式执行失败")
                emit({"event": "error", "data": {"message": str(exc)}})
            finally:
                emit(None)

        threading.Thread(target=bridge, daemon=True).start()

    return source


def _sse_response(make_source, conversation_id: int | None) -> StreamingResponse:
    """SSE 管道：事件源经 sink 直投 asyncio.Queue，心跳保活，断连置停止信号。

    make_source(emit, stop_event) 负责启动执行（sink 模式或生成器桥接回退）。
    """

    async def event_generator():
        queue: asyncio.Queue = asyncio.Queue()
        # 取消令牌带分支注册表：子代理可按 subtask 单独取消（6.7）
        stop_event = RunCancellation()
        loop = asyncio.get_running_loop()
        # 原子「检查 + 登记」放在生成器真正被迭代时：
        # ① 消除端点检查与登记之间的 TOCTOU（两个请求可能都通过早期检查）；
        # ② 客户端尚未连接就断开时不会永久占住会话（没登记就无需清理）。
        run_id = try_register_active_run(stop_event, conversation_id)
        if run_id is None:
            yield _sse(
                {
                    "event": "error",
                    "data": {
                        "message": "该会话已有正在进行的回复",
                        "code": "conversation_busy",
                    },
                }
            )
            return
        # SSE 心跳：工具执行可能 10~20 秒无事件，注释行让前端/代理知道连接存活
        HEARTBEAT_INTERVAL = 15

        def _enqueue(item) -> None:
            # 方案 A（2026-11-19）：运行结束的权威信号（done/run_suspended 事件或
            # None 哨兵）在 worker 线程侧释放会话闸门——注册表生命周期跟着事件流
            # 而不是 SSE 连接走。客户端刷新/断开后连接死了，但事件流还活着，
            # 这里不释放闸门的话，无人认领的运行会永远占住会话（409 死锁）。
            if item is None or (
                isinstance(item, dict)
                and item.get("event") in ("done", "run_suspended")
            ):
                unregister_active_run(run_id)
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                # 事件循环已关闭（客户端断开后生成器被回收）：事件已无人消费，
                # 但 unregister 已在上面执行，闸门/注册表状态不受影响
                pass

        try:
            make_source(_enqueue, stop_event)
        except Exception as exc:
            logger.exception("Agent 流式执行启动失败")
            _enqueue({"event": "error", "data": {"message": str(exc)}})
            _enqueue(None)

        # 用 asyncio.Queue 取代 asyncio.to_thread(queue.get)：后者每取一个
        # 事件就要占用一次默认执行器线程，工具轮/长回答下会频繁起落。
        # pending 跨心跳复用同一个 get 任务，慢事件不会被取消丢失。
        pending: asyncio.Task | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {pending}, timeout=HEARTBEAT_INTERVAL
                )
                if not done:
                    yield ": keepalive\n\n"
                    continue
                event = pending.result()
                pending = None
                if event is None:
                    break
                # done / run_suspended 是本次运行的最后一条业务事件：先释放会话锁
                # 再发给客户端，避免客户端立刻追问/恢复时撞上生成器 finally 尚未
                # 执行导致的 409「已有正在进行的回复」（实测 TUI 高频触发）。
                # 2026-11-19 方案 A 起 _enqueue（worker 线程侧）也会 unregister——
                # 客户端已断开时消费循环已死，闸门必须由事件流自己释放；两处
                # unregister 幂等（各自 pop 本 run_id 的映射）。
                if event.get("event") in ("done", "run_suspended"):
                    unregister_active_run(run_id)
                # session 事件里带上 run_id，客户端可据此取消本次生成
                if event.get("event") == "session":
                    event = {
                        **event,
                        "data": {**(event.get("data") or {}), "run_id": run_id},
                    }
                yield _sse(event)
        finally:
            # 方案 A（2026-11-19）：客户端断开（刷新/误关页面）不再中止整轮生成——
            # run 在后台继续跑完并落库，注册表由 _enqueue 在终止信号（done/
            # run_suspended/None 哨兵，worker 线程必发其一）时释放。这里不再置
            # stop_event 也不 unregister（提前释放会让断连后的运行失去闸门与
            # cancel 入口）。主动停止 = 前端/CLI 先调 /agent/cancel/{run_id}。
            if pending is not None and not pending.done():
                pending.cancel()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/agent/stream")
async def agent_stream(
    request: AgentChatRequest,
    service: AgentService = Depends(get_agent_service),
) -> StreamingResponse:
    """SSE 流式 Agent：session -> tool_start/tool_result -> token... -> done。

    敏感操作会以 run_suspended 收尾（本次流结束）；客户端提交决定后
    经 /agent/resume 开新流续跑同一条回答线。
    """
    kb_scope = _kb_scope_payload(request)
    _reject_if_busy(request.conversation_id, service)
    return _sse_response(
        _service_source(
            service,
            "run",
            question=request.question,
            use_web_search=request.use_web_search,
            use_knowledge_base=request.use_knowledge_base,
            conversation_id=request.conversation_id,
            template_id=request.template_id,
            system_prompt=request.system_prompt,
            db=None,
            tool_mode=request.tool_mode,
            images=request.images,
            attachments=load_attachments(get_settings(), request.attachment_ids),
            plan_only=request.plan_only,
            project_dir=request.project_dir,
            command_sandbox=request.command_sandbox,
            kb_scope=kb_scope,
            quote=request.quote.model_dump() if request.quote else None,
        ),
        request.conversation_id,
    )


@router.post("/agent/resume")
async def agent_resume(
    request: AgentResumeRequest,
    service: AgentService = Depends(get_agent_service),
) -> StreamingResponse:
    """恢复挂起的运行（敏感操作审批）：新开 SSE 流续跑同一条回答线。"""
    resume_fn = getattr(service, "resume", None)
    pending_fn = getattr(service, "has_pending_approval", None)
    payload_fn = getattr(service, "pending_approval_payload", None)
    if resume_fn is None or pending_fn is None:
        raise _code_error(400, "unsupported", "当前服务不支持恢复挂起的运行")
    if not pending_fn(request.conversation_id):
        raise _code_error(409, "no_pending", "该会话没有待确认的操作")
    if is_conversation_active(request.conversation_id):
        raise _code_error(409, "conversation_busy", "该会话已有正在进行的回复")

    decisions: dict[str, dict] = {
        d.call_id: {
            "approve": bool(d.approve),
            "reason": (d.reason or "").strip()[:200],
            "remember_session": bool(d.remember_session),
            "remember_forever": bool(d.remember_forever),
        }
        for d in request.decisions
        if d.call_id
    }
    answers: dict[str, dict] = {
        a.call_id: {
            "items": [
                {
                    "index": int(item.index),
                    "selected": [
                        str(s).strip()[:24]
                        for s in (item.selected or [])
                        if str(s).strip()
                    ][:4],
                    "other": (item.other or "").strip()[:500],
                    "skipped": bool(item.skipped),
                }
                for item in (a.items or [])
            ]
        }
        for a in request.answers
        if a.call_id
    }
    plan_payload = (
        {
            "approved": bool(request.plan.approved),
            "steps": [str(s).strip() for s in (request.plan.steps or []) if str(s).strip()],
            "reason": (request.plan.reason or "").strip()[:200],
        }
        if request.plan
        else None
    )
    # 缺省项与「停止」：以拒绝补全（停止的理由与旧阻塞式路径一致）
    payload = payload_fn(request.conversation_id) if callable(payload_fn) else None
    for op in (payload or {}).get("ops") or []:
        cid = str(op.get("call_id") or "")
        if cid and cid not in decisions:
            decisions[cid] = {
                "approve": False,
                "reason": (
                    "用户停止了回答" if request.stopped else "未提交决定，已按拒绝处理"
                ),
            }
    if (payload or {}).get("plan") and plan_payload is None:
        plan_payload = {
            "approved": False,
            "steps": [],
            "reason": (
                "用户停止了回答" if request.stopped else "未提交计划决定，已按拒绝处理"
            ),
        }

    return _sse_response(
        _service_source(
            service,
            "resume",
            conversation_id=request.conversation_id,
            decisions=decisions,
            stopped=bool(request.stopped),
            plan=plan_payload,
            answers=answers,
            db=None,
        ),
        request.conversation_id,
    )


@router.get("/agent/pending/{conversation_id}")
def agent_pending_approval(
    conversation_id: int,
    service: AgentService = Depends(get_agent_service),
) -> dict:
    """该会话的未完成状态（刷新/切换会话后前端据此恢复审批卡或提示续跑）。"""
    state_fn = getattr(service, "pending_state", None)
    if callable(state_fn):
        st = state_fn(conversation_id) or {}
        payload = st.get("_payload") or {}
        with _RUNS_LOCK:
            active_run_id = _ACTIVE_CONVS.get(conversation_id)
        return {
            "pending": bool(st.get("pending")),
            "interrupted": bool(st.get("interrupted")),
            # active：看门狗超时自动续跑等"没有挂起但仍在跑"的状态。
            # 客户端审批卡打开时需要靠它区分"已超时但后台继续"与"已结束"。
            "active": is_conversation_active(conversation_id),
            # 方案 A：断连后后台继续跑的 run_id——刷新回来的前端据此挂
            # 「后台生成中」占位，且停止按钮可以取消该运行（/agent/cancel）
            "run_id": active_run_id,
            "conversation_id": conversation_id,
            "created_at": payload.get("created_at"),
            "ops": st.get("ops") or [],
            "plan": st.get("plan"),
            "questions": st.get("questions") or [],
        }
    payload_fn = getattr(service, "pending_approval_payload", None)
    payload = payload_fn(conversation_id) if callable(payload_fn) else None
    with _RUNS_LOCK:
        active_run_id = _ACTIVE_CONVS.get(conversation_id)
    return {
        "pending": bool(payload),
        "interrupted": False,
        "active": is_conversation_active(conversation_id),
        "run_id": active_run_id,
        "conversation_id": conversation_id,
        "created_at": (payload or {}).get("created_at"),
        "ops": (payload or {}).get("ops") or [],
        "plan": (payload or {}).get("plan") or None,
        "questions": (payload or {}).get("questions") or [],
    }


@router.post("/agent/continue/{conversation_id}")
async def agent_continue(
    conversation_id: int,
    service: AgentService = Depends(get_agent_service),
) -> StreamingResponse:
    """续跑被中断的运行（进程被杀/异常退出留下的未完成任务）。"""
    continue_fn = getattr(service, "continue_run", None)
    state_fn = getattr(service, "pending_state", None)
    if continue_fn is None or state_fn is None:
        raise _code_error(400, "unsupported", "当前服务不支持续跑中断的运行")
    st = state_fn(conversation_id) or {}
    if st.get("pending"):
        raise _code_error(
            409,
            "pending_approval",
            "该会话有挂起中的确认，请先批准/拒绝（走 /agent/resume）",
        )
    if not st.get("interrupted"):
        raise _code_error(409, "no_interrupted_run", "该会话没有未完成的运行")
    if is_conversation_active(conversation_id):
        raise _code_error(409, "conversation_busy", "该会话已有正在进行的回复")
    return _sse_response(
        _service_source(
            service, "continue", conversation_id=conversation_id, db=None
        ),
        conversation_id,
    )


@router.post("/agent/abandon/{conversation_id}")
def agent_abandon(
    conversation_id: int,
    service: AgentService = Depends(get_agent_service),
) -> dict:
    """丢弃未完成的运行（删除该会话的原生检查点线程），解除新消息闸门。"""
    abandon_fn = getattr(service, "abandon_run", None)
    if abandon_fn is None:
        raise _code_error(400, "unsupported", "当前服务不支持丢弃运行")
    if is_conversation_active(conversation_id):
        raise _code_error(409, "conversation_busy", "该会话已有正在进行的回复")
    return {"ok": bool(abandon_fn(conversation_id)), "conversation_id": conversation_id}


@router.post("/agent/cancel/{run_id}")
async def agent_cancel(run_id: str) -> dict:
    """主动取消某次正在进行的流式生成（CLI 按 Ctrl+C 时调用）。"""
    cancelled, reason = request_cancel(run_id)
    return {"run_id": run_id, "cancelled": cancelled, "reason": reason}


@router.post("/agent/cancel/{run_id}/subagent/{subtask_id}")
async def agent_cancel_subagent(run_id: str, subtask_id: str) -> dict:
    """按 subtask 取消并行子代理（6.7）：只置该分支的取消信号。

    取消是协作式的：正在执行的 LLM/工具调用不会被强杀，分支在本次调用
    返回后的下一个检查点退出，merge 结果标记 cancelled 交回主代理。
    """
    with _RUNS_LOCK:
        token = _ACTIVE_RUNS.get(run_id)
    if token is None:
        return {
            "run_id": run_id,
            "subtask_id": subtask_id,
            "cancelled": False,
            "reason": "run not found",
        }
    cancel_branch = getattr(token, "cancel_branch", None)
    if not callable(cancel_branch):
        return {
            "run_id": run_id,
            "subtask_id": subtask_id,
            "cancelled": False,
            "reason": "branch cancel unsupported",
        }
    cancelled = bool(cancel_branch(subtask_id))
    return {
        "run_id": run_id,
        "subtask_id": subtask_id,
        "cancelled": cancelled,
        "reason": "" if cancelled else "subtask not found",
    }


@router.get("/agent/context/{conversation_id}")
def agent_context(
    conversation_id: int,
    service: AgentService = Depends(get_agent_service),
    db=Depends(get_db),
) -> dict:
    """上下文占用统计：消息条数 / token 预算 / 滚动摘要进度（/context 命令）。"""
    if repo.get_conversation(db, conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        return service.context.context_stats(db, conversation_id)
    except Exception as exc:
        logger.exception("上下文统计失败")
        raise HTTPException(status_code=500, detail=f"统计失败：{exc}")


@router.post("/agent/compact/{conversation_id}")
def agent_compact(
    conversation_id: int,
    service: AgentService = Depends(get_agent_service),
    db=Depends(get_db),
) -> dict:
    """手动压缩会话历史（/compact）：保留近期消息，更早的并入滚动摘要。"""
    if repo.get_conversation(db, conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        return service.context.compact_now(db, conversation_id)
    except Exception as exc:
        logger.exception("手动压缩失败")
        raise HTTPException(status_code=500, detail=f"压缩失败：{exc}")
