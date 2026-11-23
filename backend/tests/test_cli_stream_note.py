"""CLI 工具轮过程说明：撤回的过渡文本要压成一行，不换行、不超宽。"""

from __future__ import annotations

import cli.stream as stream_mod
from cli.stream import _process_note_line
from cli.term import strwidth


def test_process_note_is_single_line(monkeypatch):
    monkeypatch.setattr(stream_mod, "term_width", lambda: 80)
    line = _process_note_line("先检索知识库。\n再核对引用。")
    assert line is not None
    assert "\n" not in line
    assert "过程说明" in line
    assert "先检索知识库。 再核对引用。" in line


def test_process_note_clipped_to_width(monkeypatch):
    monkeypatch.setattr(stream_mod, "term_width", lambda: 40)
    line = _process_note_line("这是一段很长的过程说明，" * 10)
    assert line is not None
    assert strwidth(line) <= 40


def test_empty_process_note_returns_none():
    assert _process_note_line("") is None
    assert _process_note_line("   \n\t ") is None


def test_activity_copy_rotates_within_phase():
    from cli.stream import _activity_copy

    first = _activity_copy("thinking", "", 0)
    later = _activity_copy("thinking", "", 8)
    assert first == "正在思考…"
    assert later != first and later


def test_activity_copy_tool_and_finalizing():
    from cli.stream import _activity_copy

    assert _activity_copy("tool", "bash 执行中", 0) == "bash 执行中"
    assert "快好了" in _activity_copy("finalizing", "", 8)
    assert "快好了" not in _activity_copy("thinking", "", 8)


def test_activity_copy_unknown_phase_falls_back_to_detail():
    from cli.stream import _activity_copy

    assert _activity_copy("notice", "Hook 拦截：x", 0) == "Hook 拦截：x"
    assert _activity_copy("", "", 0) == "生成中…"


def test_activity_copy_retry_and_plan():
    from cli.stream import _activity_copy

    assert "重试" in _activity_copy("retry", "", 0)
    assert _activity_copy("plan", "第 2/5 步 · 核对引用", 0) == "第 2/5 步 · 核对引用"
