"""TUI 审批卡按键语义（批次 1：停止本轮 / 全部批准 / 无限等待）。"""

from __future__ import annotations

from collections import deque

from cli import render
from cli.input import Key
from cli.render import (
    ask_permission_decision,
    ask_plan_decision,
    ask_question_answer,
)
from cli.term import strip_ansi


class _Reader:
    def __init__(self, keys: list[Key]) -> None:
        self._keys = deque(keys)

    def get(self, timeout=None):
        return self._keys.popleft() if self._keys else None


def _op(**extra):
    data = {
        "call_id": "c1",
        "summary": "执行命令",
        "name": "bash",
        "arguments": {"command": "ls"},
        "rememberable": True,
    }
    data.update(extra)
    return data


def test_permission_enter_approves():
    decision = ask_permission_decision(_Reader([Key("enter")]), _op())
    assert decision == {
        "call_id": "c1",
        "approve": True,
        "reason": "",
        "remember_session": False,
        "remember_forever": False,
    }


def test_permission_ctrl_c_stops_run():
    decision = ask_permission_decision(_Reader([Key("ctrl-c")]), _op())
    assert decision == {"__stopped__": True}


def test_permission_s_stops_run():
    decision = ask_permission_decision(_Reader([Key("char", "s")]), _op())
    assert decision == {"__stopped__": True}


def test_permission_a_approves_all_only_in_multi_batch():
    single = ask_permission_decision(
        _Reader([Key("char", "a"), Key("enter")]), _op()
    )
    assert single["approve"] is True  # 单操作批次里 a 不是有效键，忽略后回车批准
    batch = ask_permission_decision(
        _Reader([Key("char", "a")]), _op(_batch_index=1, _batch_total=3)
    )
    assert batch == {"__approve_all__": True}


def test_permission_invalid_key_is_ignored():
    decision = ask_permission_decision(
        _Reader([Key("char", "9"), Key("esc")]), _op()
    )
    assert decision["approve"] is False


def test_permission_diff_unified_has_line_numbers(monkeypatch):
    monkeypatch.setattr(render, "term_width", lambda: 80)
    rows = render.permission_rows(
        {
            "name": "edit_file",
            "arguments": {
                "path": "a.py",
                "diff_lines": [
                    {"op": "-", "text": "old line"},
                    {"op": "+", "text": "new line"},
                ],
            },
        }
    )
    plain = strip_ansi("\n".join(rows))
    assert "块内行号" in plain
    assert "1 - old line" in plain
    assert "1 + new line" in plain


def test_permission_diff_side_by_side_on_wide_terminal(monkeypatch):
    monkeypatch.setattr(render, "term_width", lambda: 120)
    rows = render.permission_rows(
        {
            "name": "edit_file",
            "arguments": {
                "path": "a.py",
                "diff_lines": [
                    {"op": "-", "text": "old line"},
                    {"op": "+", "text": "new line"},
                ],
            },
        }
    )
    plain = strip_ansi("\n".join(rows))
    assert "左旧 / 右新" in plain and "块内行号" in plain
    assert "old line" in plain and "new line" in plain
    assert "│" in plain


class _RaisingReader:
    def get(self, timeout=None):
        raise KeyboardInterrupt


def test_permission_keyboard_interrupt_stops_run():
    """Windows 控制台把审批卡上的 Ctrl+C 变成 KeyboardInterrupt，也要收尾。"""
    decision = ask_permission_decision(_RaisingReader(), _op())
    assert decision == {"__stopped__": True}


def test_plan_keyboard_interrupt_stops_run():
    decision = ask_plan_decision(
        _RaisingReader(), {"summary": "计划", "steps": ["a"]}
    )
    assert decision == {"__stopped__": True}


def test_plan_ctrl_c_stops_run():
    decision = ask_plan_decision(
        _Reader([Key("ctrl-c")]), {"summary": "计划", "steps": ["a", "b"]}
    )
    assert decision == {"__stopped__": True}


# ------------------------------------------------------------------ 问答卡交互


def _question_data(**extra):
    data = {
        "question": "选哪个方案？",
        "header": "方案",
        "multi_select": False,
        "options": [
            {"label": "A", "description": "改动小"},
            {"label": "B", "description": "更彻底"},
            {"label": "C"},
        ],
    }
    data.update(extra)
    return data


def test_question_number_key_selects_then_enter_confirms():
    answer = ask_question_answer(
        _Reader([Key("char", "2"), Key("enter")]), _question_data()
    )
    assert answer == {"selected": ["B"], "other": "", "skipped": False}


def test_question_arrow_cursor_moves_and_enter_selects():
    answer = ask_question_answer(
        _Reader([Key("down"), Key("down"), Key("enter")]), _question_data()
    )
    assert answer == {"selected": ["C"], "other": "", "skipped": False}


def test_question_other_row_opens_inline_input():
    keys = [
        Key("down"),
        Key("down"),
        Key("down"),  # 3 个选项之后是「其他」
        Key("enter"),  # 进入输入
        Key("char", "先"),
        Key("char", "做"),
        Key("enter"),  # 确认输入
        Key("enter"),  # 确认该题
    ]
    answer = ask_question_answer(_Reader(keys), _question_data())
    assert answer == {"selected": [], "other": "先做", "skipped": False}


def test_question_skip_row_returns_skipped():
    keys = [Key("down")] * 4 + [Key("enter")]  # 3 选项 + 其他 -> 跳过行
    answer = ask_question_answer(_Reader(keys), _question_data())
    assert answer == {"selected": [], "other": "", "skipped": True}


def test_question_multi_select_space_accumulates():
    keys = [Key("char", " "), Key("down"), Key("char", " "), Key("enter")]
    answer = ask_question_answer(
        _Reader(keys), _question_data(multi_select=True)
    )
    assert answer == {"selected": ["A", "B"], "other": "", "skipped": False}


def test_question_esc_skips():
    answer = ask_question_answer(_Reader([Key("esc")]), _question_data())
    assert answer == {"selected": [], "other": "", "skipped": True}


def test_question_o_shortcut_enters_custom_answer():
    keys = [
        Key("char", "o"),
        Key("char", "自"),
        Key("char", "定义"),
        Key("enter"),  # 确认输入
        Key("enter"),  # 确认该题
    ]
    answer = ask_question_answer(_Reader(keys), _question_data())
    assert answer == {"selected": [], "other": "自定义", "skipped": False}


def test_question_card_renders_cursor_and_selection_marks(capsys):
    ask_question_answer(
        _Reader([Key("char", "2"), Key("enter")]), _question_data()
    )
    plain = strip_ansi(capsys.readouterr().out)
    assert "▶" in plain and "✓" in plain
    assert "其他（自己输入）" in plain and "跳过此题" in plain


def test_todo_mark_uses_check_and_circle():
    """任务清单状态符号：旧 [x]/[ ] 看起来像叉号，改为 ✔/○。"""
    from cli.format import todo_mark

    assert todo_mark(True) == "✔"
    assert todo_mark(False) == "○"
    assert todo_mark(None) == "○"
