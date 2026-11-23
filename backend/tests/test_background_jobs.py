"""后台任务能力：启动、增量输出、退出、隔离、终止（2026-11-09）。"""

from __future__ import annotations

import time
from types import SimpleNamespace

from app.tools_extra import (
    _BG_JOBS,
    _bg_kill,
    _bg_read,
    _bg_start,
    _run_command,
)


def _settings(tmp_path):
    return SimpleNamespace(
        meta_dir=tmp_path, tool_workspace="", command_sandbox="subprocess"
    )


def _wait(conv_id: int, job_id: str, timeout: float = 10.0):
    out = ""
    deadline = time.time() + timeout
    last = {"status": "running"}
    while time.time() < deadline:
        last = _bg_read(conv_id, job_id, wait_ms=500)
        out += last.get("output") or ""
        if last.get("status") != "running":
            return last, out
        time.sleep(0.1)
    return last, out


def test_background_job_output_and_exit(tmp_path):
    settings = _settings(tmp_path)
    started = _bg_start(
        settings, "echo bg-ok; sleep 0.2", "", None, None, 101
    )
    assert started.get("job_id"), started
    last, out = _wait(101, started["job_id"])
    assert last.get("status") == "exited"
    assert last.get("exit_code") == 0
    assert "bg-ok" in out
    assert last.get("elapsed_s", 0) >= 0


def test_background_job_is_isolated_by_conversation(tmp_path):
    settings = _settings(tmp_path)
    started = _bg_start(settings, "echo private", "", None, None, 102)
    _wait(102, started["job_id"])
    denied = _bg_read(999, started["job_id"])
    assert denied.get("error")
    assert "不属于当前会话" in denied["error"]


def test_background_job_kill(tmp_path):
    settings = _settings(tmp_path)
    started = _bg_start(settings, "sleep 10", "", None, None, 103)
    killed = _bg_kill(103, started["job_id"])
    assert killed.get("status") in ("killed", "exited")
    # 再读一次：进程应已退出/被标记
    last, _ = _wait(103, started["job_id"], timeout=5)
    assert last.get("status") in ("killed", "exited")
    assert _bg_read(104, started["job_id"]).get("error")


def test_background_job_per_conversation_limit(tmp_path):
    settings = _settings(tmp_path)
    jobs = []
    try:
        for _ in range(3):
            res = _bg_start(settings, "sleep 10", "", None, None, 105)
            assert res.get("job_id"), res
            jobs.append(res["job_id"])
        blocked = _bg_start(settings, "sleep 10", "", None, None, 105)
        assert blocked.get("error")
        assert "上限" in blocked["error"]
    finally:
        for jid in jobs:
            _bg_kill(105, jid)


def test_background_job_max_seconds_clamped(tmp_path):
    settings = _settings(tmp_path)
    res = _bg_start(settings, "sleep 5", "", None, None, 106, max_seconds=999999)
    assert res.get("job_id"), res
    try:
        assert _BG_JOBS[res["job_id"]].max_seconds == 21600
    finally:
        _bg_kill(106, res["job_id"])


def test_foreground_timeout_auto_backgrounds(tmp_path):
    """前台超时不再杀进程，而是自动转后台并返回 job_id。"""
    settings = SimpleNamespace(
        meta_dir=tmp_path,
        tool_workspace=str(tmp_path),
        command_sandbox="subprocess",
        command_timeout=1,
    )
    res = _run_command(
        settings, "echo before; sleep 5", "", None, None, conv_id=107
    )
    assert res.get("job_id"), res
    assert res.get("status") == "running"
    try:
        out = res.get("output") or ""
        last = {"status": "running"}
        deadline = time.time() + 5
        while time.time() < deadline:
            last = _bg_read(107, res["job_id"], wait_ms=500)
            out += last.get("output") or ""
            if "before" in out:
                break
        assert "before" in out
    finally:
        _bg_kill(107, res["job_id"])


def test_background_tool_is_sensitive_and_displayed():
    from app.permissions import describe_tool_call, display_args, is_sensitive_tool

    args = {"command": "npm run dev", "cwd": "."}
    assert is_sensitive_tool("bash_background", args) is True
    assert display_args("bash_background", args) == {
        "command": "npm run dev",
        "cwd": ".",
    }
    assert "后台" in describe_tool_call("bash_background", args)
