"""TUI 审批超时感知：HTTP 错误详情、pending 轮询与重新同步。"""

from __future__ import annotations

import io
import json
import threading
import urllib.error

from cli import pending as pending_mod
from cli.http import http_error_detail


class _Screen:
    def __init__(self):
        self.lines: list[str] = []

    def newline(self):
        self.lines.append("")

    def write(self, text):
        self.lines.append(text)


def test_http_error_detail_reads_json_body():
    body = io.BytesIO(
        json.dumps({"detail": "该会话没有待确认的操作"}, ensure_ascii=False).encode(
            "utf-8"
        )
    )
    exc = urllib.error.HTTPError("http://x", 409, "Conflict", {}, body)
    assert "该会话没有待确认的操作" in http_error_detail(exc)


def test_watch_pending_marks_stale_when_pending_disappears(monkeypatch):
    monkeypatch.setattr(
        pending_mod,
        "fetch_pending",
        lambda base, cid: {"pending": False, "interrupted": False, "active": True},
    )
    stale = threading.Event()
    stop = threading.Event()
    pending_mod.watch_pending("http://x", 1, 1.0, stale, stop)
    assert stale.is_set()


def test_watch_pending_marks_stale_on_new_batch(monkeypatch):
    monkeypatch.setattr(
        pending_mod,
        "fetch_pending",
        lambda base, cid: {"pending": True, "created_at": 2.0},
    )
    stale = threading.Event()
    stop = threading.Event()
    pending_mod.watch_pending("http://x", 1, 1.0, stale, stop)
    assert stale.is_set()


def test_wait_pending_prefers_active_over_interrupted(monkeypatch):
    states = iter(
        [
            {"pending": False, "interrupted": True, "active": True},
            {"pending": False, "interrupted": False, "active": False},
        ]
    )
    monkeypatch.setattr(pending_mod, "fetch_pending", lambda base, cid: next(states))
    monkeypatch.setattr(
        pending_mod, "last_assistant_text", lambda base, cid: "已完成"
    )
    monkeypatch.setattr(pending_mod.time, "sleep", lambda seconds: None)

    outcome = pending_mod.wait_pending_or_done("http://x", 1, _Screen())
    assert outcome["auto_done"] is True
    assert outcome["text"] == "已完成"


def test_collect_stops_run_on_keyboard_interrupt(monkeypatch):
    monkeypatch.setattr(
        pending_mod,
        "fetch_pending",
        lambda base, cid: {"pending": True, "created_at": 1.0},
    )

    def ask_op(reader, op, stale_event=None):
        raise KeyboardInterrupt

    payload = {
        "conversation_id": 1,
        "created_at": 1.0,
        "ops": [{"call_id": "c1"}],
        "plan": None,
    }
    outcome = pending_mod.collect_suspended_decisions(
        "http://x", 1, payload, None, _Screen(), ask_op, lambda *a, **k: None
    )
    assert outcome["resume_body"]["stopped"] is True


def test_collect_stops_run_on_stop_marker(monkeypatch):
    monkeypatch.setattr(
        pending_mod,
        "fetch_pending",
        lambda base, cid: {"pending": True, "created_at": 1.0},
    )

    def ask_op(reader, op, stale_event=None):
        return {"__stopped__": True}

    payload = {
        "conversation_id": 1,
        "created_at": 1.0,
        "ops": [{"call_id": "c1"}, {"call_id": "c2"}],
        "plan": None,
    }
    outcome = pending_mod.collect_suspended_decisions(
        "http://x",
        1,
        payload,
        None,
        _Screen(),
        ask_op,
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("本用例没有计划")),
    )
    body = outcome["resume_body"]
    assert body["stopped"] is True
    assert body["decisions"] == []


def test_collect_approve_all_fills_remaining(monkeypatch):
    monkeypatch.setattr(
        pending_mod,
        "fetch_pending",
        lambda base, cid: {"pending": True, "created_at": 1.0},
    )
    seen = {}

    def ask_op(reader, op, stale_event=None):
        seen["batch"] = (op["_batch_index"], op["_batch_total"])
        return {"__approve_all__": True}

    payload = {
        "conversation_id": 1,
        "created_at": 1.0,
        "ops": [{"call_id": "c1"}, {"call_id": "c2"}],
        "plan": None,
    }
    outcome = pending_mod.collect_suspended_decisions(
        "http://x", 1, payload, None, _Screen(), ask_op, lambda *a, **k: None
    )
    body = outcome["resume_body"]
    assert seen["batch"] == (1, 2)
    assert body["stopped"] is False
    assert [d["call_id"] for d in body["decisions"]] == ["c1", "c2"]
    assert all(d["approve"] for d in body["decisions"])
    assert "plan" not in body


def test_collect_plan_stop_omits_plan(monkeypatch):
    monkeypatch.setattr(
        pending_mod,
        "fetch_pending",
        lambda base, cid: {"pending": True, "created_at": 1.0},
    )

    def ask_plan(reader, plan, stale_event=None):
        return {"__stopped__": True}

    payload = {
        "conversation_id": 1,
        "created_at": 1.0,
        "ops": [],
        "plan": {"summary": "计划", "steps": ["a"]},
    }
    outcome = pending_mod.collect_suspended_decisions(
        "http://x", 1, payload, None, _Screen(), lambda *a, **k: None, ask_plan
    )
    body = outcome["resume_body"]
    assert body["stopped"] is True
    assert "plan" not in body


def test_collect_questions_builds_answers(monkeypatch):
    monkeypatch.setattr(
        pending_mod,
        "fetch_pending",
        lambda base, cid: {"pending": True, "created_at": 1.0},
    )
    asked = []

    def ask_q(reader, data, stale_event=None):
        asked.append(data["question"])
        return {"selected": ["A"], "other": "", "skipped": False}

    payload = {
        "conversation_id": 1,
        "created_at": 1.0,
        "ops": [],
        "plan": None,
        "questions": [
            {
                "call_id": "q1",
                "questions": [
                    {"question": "选哪个？", "options": [{"label": "A"}, {"label": "B"}]},
                    {"question": "要测试吗？", "options": [{"label": "要"}, {"label": "不要"}]},
                ],
            }
        ],
    }
    outcome = pending_mod.collect_suspended_decisions(
        "http://x",
        1,
        payload,
        None,
        _Screen(),
        lambda *a, **k: None,
        lambda *a, **k: None,
        ask_q,
    )
    body = outcome["resume_body"]
    assert asked == ["选哪个？", "要测试吗？"]
    assert body["answers"][0]["call_id"] == "q1"
    assert [i["index"] for i in body["answers"][0]["items"]] == [0, 1]
    assert body["answers"][0]["items"][0]["selected"] == ["A"]
    assert body["stopped"] is False


def test_collect_resyncs_after_stale(monkeypatch):
    monkeypatch.setattr(
        pending_mod,
        "fetch_pending",
        lambda base, cid: {
            "pending": False,
            "interrupted": False,
            "active": False,
            "ops": [],
            "plan": None,
        },
    )
    monkeypatch.setattr(
        pending_mod, "last_assistant_text", lambda base, cid: "最终回答"
    )

    def ask_op(reader, op, stale_event=None):
        stale_event.set()
        return {"__stale__": True}

    def ask_plan(reader, plan, stale_event=None):
        raise AssertionError("本用例没有计划")

    payload = {
        "conversation_id": 1,
        "created_at": 1.0,
        "ops": [{"call_id": "c1"}],
        "plan": None,
    }
    outcome = pending_mod.collect_suspended_decisions(
        "http://x", 1, payload, None, _Screen(), ask_op, ask_plan
    )
    assert outcome["auto_done"] is True
    assert outcome["text"] == "最终回答"
