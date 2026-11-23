"""挂起审批的轮询与重新同步（TUI 交互层）。

服务端 permission_timeout 到点会自动拒绝并继续执行；本模块在等待用户决定时
轮询 /agent/pending，发现批次已被处理就重新同步，避免把过期决定提交上去
（否则会撞 409「该会话没有待确认的操作」）。

只依赖 cli.term / cli.http（单向向下）；审批弹窗函数由调用方注入，避免与
cli_agent 形成循环依赖。
"""

from __future__ import annotations

import threading
import time

from cli.http import fetch_conversation_messages, fetch_pending
from cli.term import Screen, paint


def watch_pending(
    base_url: str,
    conversation_id: int,
    batch_created: float,
    stale: threading.Event,
    stop: threading.Event,
) -> None:
    """审批等待期间轮询：批次被服务端处理（超时自动拒绝）或换新批次时置 stale。"""
    while not stop.is_set() and not stale.is_set():
        state = fetch_pending(base_url, conversation_id)
        if state:
            if state.get("interrupted") or not state.get("pending"):
                stale.set()
                return
            created = float(state.get("created_at") or 0)
            if batch_created and created and abs(created - batch_created) > 1e-6:
                stale.set()
                return
        stop.wait(2)


def last_assistant_text(base_url: str, conversation_id: int) -> str:
    messages = fetch_conversation_messages(base_url, conversation_id, limit=6)
    for msg in reversed(messages):
        if str(msg.get("role") or "") == "assistant":
            text = str(msg.get("content") or "").strip()
            if text:
                return text
    return ""


def wait_pending_or_done(
    base_url: str, conversation_id: int, screen: Screen, max_wait: int | None = None
) -> dict:
    """审批批次被服务端处理后：等新批次 / 中断 / 自动跑完，返回下一步。

    max_wait=None（默认）表示无限等待——审批本身已改为无限等待，后端续跑
    可能持续很久，客户端不应再用固定时长放弃。
    """
    waited = 0
    last_note = 0
    while max_wait is None or waited < max_wait:
        state = fetch_pending(base_url, conversation_id)
        if state:
            if state.get("pending"):
                return {
                    "pending": True,
                    "payload": {
                        "conversation_id": conversation_id,
                        "ops": state.get("ops") or [],
                        "plan": state.get("plan"),
                        "questions": state.get("questions") or [],
                        "created_at": state.get("created_at"),
                    },
                }
            if state.get("active"):
                # 看门狗续跑期间 interrupted 也会为 True：先判 active，
                # 否则会把"后台正在跑"误报成"运行已中断"。
                pass
            elif state.get("interrupted"):
                return {"interrupted": True}
            else:
                return {
                    "auto_done": True,
                    "text": last_assistant_text(base_url, conversation_id),
                }
        if waited and waited % 30 == 0 and waited != last_note:
            last_note = waited
            screen.write(paint(f"   后端仍在继续执行（已等待 {waited}s）…", "dim"))
        time.sleep(2)
        waited += 2
    return {"timeout": True}


def _approve(op: dict) -> dict:
    """构造一条“批准”决定（全部批准时用于剩余操作）。"""
    return {
        "call_id": str(op.get("call_id") or op.get("id") or ""),
        "approve": True,
        "reason": "",
        "remember_session": False,
        "remember_forever": False,
    }


def collect_suspended_decisions(
    base_url: str,
    conversation_id: int,
    payload: dict,
    reader,
    screen: Screen,
    ask_op,
    ask_plan,
    ask_question=None,
) -> dict:
    """收集一批审批；期间轮询 pending，批次被处理/换批时重新同步。

    返回：
    - {"resume_body": {...}} 用户已决定，调用方发 /agent/resume
      （stopped=True 表示用户在审批卡上按了 s/Ctrl+C：停止本轮）；
    - {"auto_done": True, "text": ...} 服务端处置后已跑完；
    - {"interrupted": True} 运行中断，可 /continue；
    - {"timeout": True} 客户端等待超限（仅显式传入 max_wait 时）。
    """
    while True:
        ops = payload.get("ops") or []
        plan = payload.get("plan") or None
        questions = payload.get("questions") or []
        question_count = sum(len(q.get("questions") or []) for q in questions)
        batch_created = float(payload.get("created_at") or 0)
        screen.newline()
        parts = []
        if ops:
            parts.append(f"{len(ops)} 项敏感操作")
        if plan:
            parts.append("执行计划")
        if question_count:
            parts.append(f"{question_count} 个问题")
        label = " + ".join(parts) or "待确认"
        if question_count and not ops and not plan:
            screen.write(paint(f"⏸ 等待回答：{question_count} 个问题", "yellow"))
        else:
            screen.write(paint(f"⏸ 等待确认：{label}", "yellow"))

        stale = threading.Event()
        stop = threading.Event()
        watcher = threading.Thread(
            target=watch_pending,
            args=(base_url, conversation_id, batch_created, stale, stop),
            daemon=True,
        )
        watcher.start()
        decisions: list[dict] = []
        answers: list[dict] = []
        aborted = False
        stopped = False
        plan_decision = None
        try:
            total = len(ops)
            for idx, op in enumerate(ops):
                view = dict(op)
                view["_batch_index"] = idx + 1
                view["_batch_total"] = total
                try:
                    decision = ask_op(reader, view, stale_event=stale)
                except KeyboardInterrupt:
                    stopped = True
                    break
                if decision.get("__stale__"):
                    aborted = True
                    break
                if decision.get("__stopped__"):
                    stopped = True
                    break
                if decision.get("__approve_all__"):
                    decisions.extend(_approve(o) for o in ops[idx:])
                    break
                decisions.append(decision)
            if plan and not aborted and not stopped:
                try:
                    plan_decision = ask_plan(reader, plan, stale_event=stale)
                except KeyboardInterrupt:
                    stopped = True
                    plan_decision = None
                else:
                    if plan_decision.get("__stale__"):
                        aborted = True
                        plan_decision = None
                    elif plan_decision.get("__stopped__"):
                        stopped = True
                        plan_decision = None
            if ask_question and questions and not aborted and not stopped:
                for req in questions:
                    call_id = str(req.get("call_id") or "")
                    items: list[dict] = []
                    for idx, q in enumerate(req.get("questions") or []):
                        try:
                            answer = ask_question(
                                reader,
                                {**q, "call_id": call_id, "index": idx},
                                stale_event=stale,
                            )
                        except KeyboardInterrupt:
                            stopped = True
                            break
                        if answer.get("__stale__"):
                            aborted = True
                            break
                        if answer.get("__stopped__"):
                            stopped = True
                            break
                        items.append(
                            {
                                "index": idx,
                                "selected": answer.get("selected") or [],
                                "other": answer.get("other") or "",
                                "skipped": bool(answer.get("skipped")),
                            }
                        )
                    if aborted or stopped:
                        break
                    answers.append({"call_id": call_id, "items": items})
        finally:
            stop.set()

        if not aborted:
            body = {
                "conversation_id": payload.get("conversation_id")
                or conversation_id,
                "decisions": decisions,
                "stopped": stopped,
            }
            if plan and not stopped:
                body["plan"] = plan_decision
            if questions and answers and not stopped:
                body["answers"] = answers
            return {"resume_body": body}

        screen.write(
            paint("⏰ 该批审批已由服务端/其他客户端处理，正在重新同步…", "yellow")
        )
        outcome = wait_pending_or_done(base_url, conversation_id, screen)
        if outcome.get("pending"):
            payload = outcome["payload"]
            continue
        return outcome
