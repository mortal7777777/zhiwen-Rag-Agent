"""终端客户端纯函数单测：宽度/换行/Markdown/补全/历史（无 I/O、无线程）。"""

from __future__ import annotations

import io
import sys

import cli_agent as c
from cli import input as cli_input
from cli import term as cli_term


# ------------------------------------------------------------------ 宽度与换行


def test_strwidth_ascii_cjk_ansi_tab():
    assert c.strwidth("abc") == 3
    assert c.strwidth("你好") == 4
    assert c.strwidth("a你b") == 4
    assert c.strwidth("\x1b[31mx\x1b[0m") == 1
    assert c.strwidth("\t") == 4


def test_clip_cjk_aware():
    # ASCII 省略号（3 列）：中文终端不会把 U+2026 按 2 列渲染导致边框错位
    assert c.clip("你好世界", 5) == "你..."
    assert c.clip("你好世界", 8) == "你好世界"
    assert c.clip("abcdef", 4) == "a..."
    assert c.strwidth(c.clip("abcdefgh", 7)) == 7


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
    # USE_COLOR 的归属已移到 cli.term（物理拆分后打补丁要打到真正读它的模块）
    monkeypatch.setattr(cli_term, "USE_COLOR", True)
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


# 2026-11-19：表格改为"缓存整表后按全部行定宽"（否则 310ms 对 42ms 会被
# mid-token 折行），因此以下用例改为 feed(...) + flush() 取整表输出。
def test_markdown_table_separator_matches_row_width():
    m = c.MarkdownStream(80)
    out = c.strip_ansi(m.feed("| 列1 | 列2 |\n|---|---|\n| a | b |\n") + m.flush())
    lines = [line for line in out.splitlines() if line.strip()]
    assert len({c.strwidth(line) for line in lines}) == 1


def test_table_long_cell_wraps_instead_of_clipping():
    """单元格超宽要按列宽换行完整显示；旧实现 clip 成省略号会丢内容。"""
    m = c.MarkdownStream(80)
    long_text = (
        "这是一段较长的说明文字，用来验证表格单元格会按列宽自动换行，"
        "而不是被省略号截断。"
    )
    plain = c.strip_ansi(m.feed(f"| 说明 |\n|---|\n| {long_text} |\n") + m.flush())
    compact = plain.replace("│", "").replace("\n", "").replace(" ", "")
    assert long_text in compact
    assert "..." not in plain
    widths = {c.strwidth(line) for line in plain.splitlines() if line.strip()}
    assert len(widths) == 1


def test_table_wrapped_row_keeps_borders_aligned():
    m = c.MarkdownStream(80)
    plain = c.strip_ansi(
        m.feed("| 序号 | 说明 |\n|---|---|\n| 1 | " + "长文本" * 20 + " |\n") + m.flush()
    )
    widths = {c.strwidth(line) for line in plain.splitlines() if line.strip()}
    assert len(widths) == 1


def test_table_columns_adapt_to_content():
    """列宽按表头 + 首行数据分配：序号/备注窄，说明列宽。"""
    m = c.MarkdownStream(80)
    plain = c.strip_ansi(
        m.feed(
            "| 序号 | 说明 | 备注 |\n"
            "|---|---|---|\n"
            "| 1 | 这是一段较长的说明文字，用来验证列宽会按内容分配而不是平均分。 | 正常 |\n"
        )
        + m.flush()
    )
    header = next(line for line in plain.splitlines() if "序号" in line)
    parts = header.split("│")[1:-1]
    widths = [c.strwidth(p) for p in parts]
    assert widths[0] < widths[1] and widths[2] < widths[1]
    assert widths[0] <= 10


def test_table_header_only_is_flushed_at_end():
    m = c.MarkdownStream(80)
    plain = c.strip_ansi(m.feed("| 表头一 | 表头二 |\n") + m.flush())
    assert "表头一" in plain and "│" in plain
    assert "| 表头一" not in plain


def test_table_header_flushed_when_followed_by_text():
    m = c.MarkdownStream(80)
    plain = c.strip_ansi(m.feed("| 表头 |\n正文\n"))
    assert "表头" in plain and "正文" in plain


def test_code_block_not_whole_line_dim(monkeypatch):
    monkeypatch.setattr(cli_term, "USE_COLOR", True)
    out = c.MarkdownStream(80).feed("```\nplain code\n```\n```unknown\nother code\n```\n")
    for line in out.splitlines():
        if "plain code" in line or "other code" in line:
            assert "\x1b[2m" not in line  # 非 token 正文不再整段灰色


def test_single_line_fence_does_not_swallow_rest():
    m = c.MarkdownStream(80)
    out = m.feed("```code```\n后面的正文\n")
    assert m._in_fence is False
    assert "┌─" not in out
    assert "后面的正文" in c.strip_ansi(out)


def test_table_rendered_consistently_when_streamed_in_pieces():
    """表格行被分片到达时也必须整体走表格渲染，不能退化成原始 Markdown。"""
    m = c.MarkdownStream(80)
    out = ""
    for chunk in ["| 项目 | 结", "果 |\n", "|---|", "---|\n", "| a | b |\n"]:
        out += m.feed(chunk)
        if m.has_partial():
            out += m.partial()
    out += m.flush()
    plain = c.strip_ansi(out)
    assert "│ 项目" in plain and "│ 结果" in plain
    assert "|---" not in plain
    lines = [line for line in plain.splitlines() if line.strip()]
    assert len({c.strwidth(line) for line in lines}) == 1


def test_heading_levels_and_nested_lists_are_distinct():
    m = c.MarkdownStream(80)
    plain = c.strip_ansi(m.feed("# 一级\n## 二级\n### 三级\n- 顶层\n  - 子项\n"))
    assert "█ 一级" in plain and "▌ 二级" in plain and "▸ 三级" in plain
    assert "  • 顶层" in plain and "    • 子项" in plain


def test_markdown_stream_table_and_hr():
    m = c.MarkdownStream(60)
    out = m.feed("| 列1 | 列2 |\n|---|---|\n| a | b |\n---\n")
    assert "│" in out
    assert "├" in out
    assert "─" * 3 in out


def test_markdown_stream_partial_no_double_print():
    m = c.MarkdownStream(60)
    assert m.feed("这是第一段") == ""
    assert not m.has_partial()  # 行缓冲：半行不提前输出
    assert m.partial() == ""
    rest = m.feed("的结尾\n")
    assert "这是第一段的结尾" in c.strip_ansi(rest)  # 完整行一次性渲染
    assert m.flush() == ""


def test_markdown_stream_wrap_respects_width():
    m = c.MarkdownStream(20)
    out = m.feed("这是一段非常非常长的文本需要被换行显示\n")
    for line in out.splitlines():
        assert c.strwidth(line) <= 20


# ------------------------------------------------------------------ 增量流式撤回


def test_visible_rows_counts_wrap_cjk_and_ansi():
    assert c.visible_rows("abc", 80) == 1
    assert c.visible_rows("abc\n", 80) == 2  # 结尾换行：光标停在下一行
    assert c.visible_rows("中文" * 30, 20) == 6  # 60 全角字 = 120 列 → 6 行
    assert c.visible_rows(c.paint("x" * 100, "red"), 40) == 3  # ANSI 不计宽
    assert c.visible_rows("a\n\nb", 80) == 3  # 空行也占一行


def test_markdown_stream_reset_discards_partial():
    m = c.MarkdownStream(80)
    assert m.feed("半截过渡文本") == ""
    assert not m.has_partial()
    assert m.partial() == ""
    m.reset()
    assert not m.has_partial()
    assert m.partial() == ""  # 撤回后残留片段不会再被打印
    assert m.flush() == ""


def test_screen_erase_block_moves_up_and_clears(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(c.sys, "stdout", buf)
    screen = c.Screen()
    screen.on_line = True
    screen.erase_block(3)
    assert buf.getvalue() == "\033[2A\r\033[J"
    assert screen.on_line is False

    buf.seek(0)
    buf.truncate()
    screen.erase_block(1)  # 单行块：只清当前行
    assert buf.getvalue() == "\r\033[J"


# ------------------------------------------------------------------ 补全与工具参数


def test_complete_commands():
    assert c.complete("/too") == (["/tools"], "ls")
    matches, ghost = c.complete("/to")
    assert "/tools" in matches and "/todos" in matches
    assert ghost == ""
    # /exit 与 /export 同前缀：无幽灵补全，补到 /exi 才唯一
    assert c.complete("/ex")[1] == ""
    assert c.complete("/exi")[1] == "t"


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

    monkeypatch.setattr(cli_input.KeyReader, "get", fake_get)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def _boom(_history):
        raise RuntimeError("prompt_toolkit 不可用（测试强制 legacy）")

    # read_line 住在 cli.input：补丁必须打到它所在模块的全局名上
    monkeypatch.setattr(cli_input, "make_prompt_session", _boom)
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
