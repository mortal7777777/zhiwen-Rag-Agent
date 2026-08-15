"""终端客户端纯函数单测：宽度/换行/Markdown/补全/历史（无 I/O、无线程）。"""

from __future__ import annotations

import sys

import cli_agent as c


# ------------------------------------------------------------------ 宽度与换行


def test_strwidth_ascii_cjk_ansi_tab():
    assert c.strwidth("abc") == 3
    assert c.strwidth("你好") == 4
    assert c.strwidth("a你b") == 4
    assert c.strwidth("\x1b[31mx\x1b[0m") == 1
    assert c.strwidth("\t") == 4


def test_clip_cjk_aware():
    assert c.clip("你好世界", 5) == "你好…"
    assert c.clip("你好世界", 8) == "你好世界"
    assert c.clip("abcdef", 4) == "abc…"


def test_pad_right():
    assert c.pad_right("你好", 6) == "你好  "


def test_wrap_styled_cjk_and_ansi():
    lines = c.wrap_styled("这是一段很长的中文文本", 8)
    assert lines
    assert all(c.strwidth(line) <= 8 for line in lines)
    joined = "".join(lines)
    assert "中文" in joined

    styled = c.wrap_styled("\x1b[31mred\x1b[0m word", 8)
    assert any("red" in c.strip_ansi(line) for line in styled)


def test_wrap_styled_indent_and_hard_break():
    lines = c.wrap_styled("abcdefghij", 6)
    assert lines == ["abcdef", "ghij"]
    lines = c.wrap_styled("aa bb cc dd", 8, first_indent=2, indent=2)
    assert lines == ["  aa bb", "  cc dd"]


def test_fmt_helpers():
    assert c.fmt_dur(0.5) == "500ms"
    assert c.fmt_dur(95.2) == "1m35s"
    assert c.fmt_dur(3725) == "1h02m"
    assert c.fmt_num(999) == "999"
    assert c.fmt_num(1234) == "1.2k"
    assert "1.2k" in c.fmt_tokens({"input_tokens": 1234, "output_tokens": 56})
    assert c.fmt_tokens({"prompt_tokens": 1, "completion_tokens": 2}) == "输入 1 · 输出 2"


# ------------------------------------------------------------------ 安全


def test_safe_text_strips_escape_injection():
    cleaned = c.safe_text("正常 \x1b[31m注入\x1b[0m 文本")
    assert "\x1b" not in cleaned
    assert "正常" in cleaned and "注入" in cleaned and "文本" in cleaned
    assert c.safe_text("a\rb\x07") == "ab"
    assert c.safe_text("行\n换行") == "行\n换行"


# ------------------------------------------------------------------ Markdown


def test_inline_md(monkeypatch):
    monkeypatch.setattr(c, "USE_COLOR", True)
    out = c.inline_md("`code` **粗** *斜* [链接](http://x)")
    plain = c.strip_ansi(out)
    assert "code" in plain and "粗" in plain and "斜" in plain and "链接" in plain
    assert "\x1b[" in out  # 上色生效


def test_markdown_stream_headings_list_quote():
    m = c.MarkdownStream(60)
    out = m.feed("## 标题\n\n- 项目一\n- 项目二\n\n> 引用内容\n")
    assert "标题" in out
    assert "• 项目一" in out
    assert "│ 引用内容" in c.strip_ansi(out)


def test_markdown_stream_code_fence():
    m = c.MarkdownStream(60)
    out = m.feed("```python\nprint(1)\n```\n结尾\n")
    assert "┌─ python" in out
    assert "print(1)" in c.strip_ansi(out)
    assert "└─" in out
    assert "结尾" in out


def test_markdown_stream_table_and_hr():
    m = c.MarkdownStream(60)
    out = m.feed("| 列1 | 列2 |\n|---|---|\n| a | b |\n---\n")
    assert "│" in out
    assert "├" in out
    assert "─" * 3 in out


def test_markdown_stream_partial_no_double_print():
    m = c.MarkdownStream(60)
    assert m.feed("这是第一段") == ""
    assert m.has_partial()
    partial = m.partial()
    assert "这是第一段" in c.strip_ansi(partial)
    rest = m.feed("的结尾\n")
    assert "这是第一段" not in c.strip_ansi(rest)  # 已打印前缀不再重复
    assert "的结尾" in c.strip_ansi(rest)
    assert m.flush() == ""


def test_markdown_stream_wrap_respects_width():
    m = c.MarkdownStream(20)
    out = m.feed("这是一段非常非常长的文本需要被换行显示\n")
    for line in out.splitlines():
        assert c.strwidth(line) <= 20


# ------------------------------------------------------------------ 补全与工具参数


def test_complete_commands():
    assert c.complete("/too") == (["/tools"], "ls")
    matches, ghost = c.complete("/to")
    assert "/tools" in matches and "/todos" in matches
    assert ghost == ""
    assert c.complete("/ex")[1] == "it"  # /exit


def test_complete_tools_argument():
    matches, ghost = c.complete("/tools a")
    assert "auto" in matches
    assert ghost == "uto"
    assert c.complete("普通文本") == ([], "")


def test_input_layout_single_line_and_wrap():
    # 短输入单行：prompt 宽 2 + 内容 4
    assert c.input_layout("你好", 2, 10) == (1, 0, 6)
    # 长输入跨行：42 显示宽 / 20 列 → 3 行，光标在最后一行第 3 列（含 ANSI 零宽）
    rows, row, col = c.input_layout("a" * 40, 40, 20)
    assert (rows, row, col) == (3, 2, 2)
    # 光标在中间位置
    assert c.input_layout("abcdef", 3, 10) == (1, 0, 5)


def test_render_tool_args():
    args = {"path": "C:/very/long/path/that/keeps/going", "cmd": "python x.py"}
    out = c.render_tool_args(args)
    assert c.strwidth(out) <= 102
    assert "path=" in out and "cmd=" in out
    assert c.render_tool_args(None) == ""
    assert "hello" in c.render_tool_args("hello")


# ------------------------------------------------------------------ 会话/历史持久化


def test_session_roundtrip(tmp_path):
    c.save_session(str(tmp_path), 12)
    history = c.load_session_history(str(tmp_path))
    assert history[0]["conversation_id"] == 12
    c.save_session(str(tmp_path), 34)
    history = c.load_session_history(str(tmp_path))
    assert [h["conversation_id"] for h in history] == [34, 12]


def test_history_roundtrip(tmp_path):
    assert c.load_history(str(tmp_path)) == []
    c.save_history(str(tmp_path), ["问题一", "问题二"])
    assert c.load_history(str(tmp_path)) == ["问题一", "问题二"]


# ------------------------------------------------------------------ 键盘中断安全


def _force_legacy_read_line(monkeypatch, fake_get):
    """强制 read_line 走 legacy 键处理路径（TTY 模拟）：
    - sys.stdin.isatty() → True（绕过管道分支）
    - make_prompt_session → 抛异常（绕过 prompt_toolkit，进入键循环）
    - KeyReader.get → fake（模拟键盘事件/Ctrl+C 信号路径）
    """
    import cli_agent as c_mod

    monkeypatch.setattr(c.KeyReader, "get", fake_get)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def _boom(_history):
        raise RuntimeError("prompt_toolkit 不可用（测试强制 legacy）")

    monkeypatch.setattr(c_mod, "make_prompt_session", _boom)
    return c_mod


def test_read_line_ctrl_c_at_empty_prompt_stays_in_cli(monkeypatch):
    """提示符下 Ctrl+C（Windows 信号路径 KeyboardInterrupt）必须转成
    Interrupted 而不是逃逸杀死整个 CLI。"""

    def fake_get(self, timeout=None):
        raise KeyboardInterrupt()

    c_mod = _force_legacy_read_line(monkeypatch, fake_get)
    try:
        c_mod.read_line(c.KeyReader(), [])
        assert False, "应抛出 Interrupted"
    except c.Interrupted as exc:
        assert exc.had_text is False


def test_read_line_ctrl_c_with_text_keeps_input(monkeypatch):
    """输入框里有内容时 Ctrl+C 转 Interrupted(had_text=True)。"""

    def fake_get(self, timeout=None):
        # 第一次返回一个字符，第二次抛 KeyboardInterrupt（Windows 信号路径）
        if not hasattr(self, "_n"):
            self._n = 0
        if self._n == 0:
            self._n = 1
            return c.Key("char", "你")
        raise KeyboardInterrupt()

    c_mod = _force_legacy_read_line(monkeypatch, fake_get)
    try:
        c_mod.read_line(c.KeyReader(), [])
        assert False, "应抛出 Interrupted"
    except c.Interrupted as exc:
        assert exc.had_text is True
