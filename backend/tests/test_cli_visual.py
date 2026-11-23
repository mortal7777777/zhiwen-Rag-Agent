"""视觉层：主题/NO_COLOR/ASCII、代码高亮、分页器纯逻辑（批次 3）。"""

from __future__ import annotations

from cli import format as fmt
from cli import pager
from cli import term


# ------------------------------------------------------------------ 主题


def test_detect_theme_from_env(monkeypatch):
    monkeypatch.setenv("ZHIWEN_THEME", "light")
    assert term._detect_theme() == "light"
    monkeypatch.setenv("ZHIWEN_THEME", "dark")
    assert term._detect_theme() == "dark"


def test_detect_theme_from_colorfgbg(monkeypatch):
    monkeypatch.delenv("ZHIWEN_THEME", raising=False)
    monkeypatch.setenv("COLORFGBG", "15;0")
    assert term._detect_theme() == "dark"
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert term._detect_theme() == "light"


def test_detect_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("ZHIWEN_THEME", raising=False)
    assert term._detect_no_color() is False
    monkeypatch.setenv("NO_COLOR", "1")
    assert term._detect_no_color() is True
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("ZHIWEN_THEME", "none")
    assert term._detect_no_color() is True


def test_light_theme_uses_256_colors(monkeypatch):
    monkeypatch.setattr(term, "USE_COLOR", True)
    monkeypatch.setattr(term, "THEME", "light")
    out = term.paint("x", "yellow")
    assert "38;5;130" in out


def test_boxed_all_lines_same_display_width(monkeypatch, capsys):
    from cli import render

    monkeypatch.setattr(render, "term_width", lambda: 40)
    render.boxed(
        ["短行", "这是一段很长的中文内容，会被裁剪到终端宽度以内"],
        "标题",
        "cyan",
    )
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) >= 4
    assert {term.strwidth(line) for line in lines} == {40}


def test_boxed_long_title_still_aligned(monkeypatch, capsys):
    from cli import render

    monkeypatch.setattr(render, "term_width", lambda: 30)
    render.boxed(["x"], "一个非常非常非常长的标题", "cyan")
    lines = capsys.readouterr().out.splitlines()
    widths = {term.strwidth(line) for line in lines}
    assert len(widths) == 1 and widths.pop() <= 30


def test_ascii_mode_replaces_glyphs(monkeypatch):
    monkeypatch.setattr(term, "ASCII_MODE", True)
    out = term.paint("⏺ ╭─ │ 💭", "")
    assert out == "* +- | [think]"


def test_osc8_link_is_zero_width_and_clip_balances(monkeypatch):
    monkeypatch.setattr(term, "HYPERLINKS", True)
    out = term.link("显示", "https://example.com")
    assert "https://example.com" in out
    assert term.strwidth(out) == term.strwidth("显示")
    clipped = term.clip(out, 2)
    assert "https://example.com" in clipped
    assert clipped.endswith("\x1b]8;;\x1b\\")


def test_notify_and_title_sequences(monkeypatch, capsys):
    monkeypatch.setattr(term, "NOTIFY", True)
    monkeypatch.setattr(term, "USE_COLOR", True)
    term.notify("知问", "完成")
    term.set_title("知问 · 生成中")
    out = capsys.readouterr().out
    assert "\033]9;知问: 完成\007" in out
    assert "\033]0;知问 · 生成中\007" in out


# ------------------------------------------------------------------ 代码高亮


def test_highlight_code_preserves_text_and_colors(monkeypatch):
    monkeypatch.setattr(term, "USE_COLOR", True)
    line = 'def foo(x): return "s"  # note'
    out = fmt.highlight_code(line, "python")
    assert "\x1b[" in out
    assert term.strip_ansi(out) == line


def test_highlight_unknown_language_is_dim(monkeypatch):
    monkeypatch.setattr(term, "USE_COLOR", True)
    out = fmt.highlight_code("plain text", "unknown-lang")
    assert term.strip_ansi(out) == "plain text"


def test_markdown_fence_tracks_language():
    m = fmt.MarkdownStream(80)
    out = m.feed("```python\ndef f():\n    return 1\n```\n")
    assert "┌─ python" in out
    assert "└─" in out
    assert "return 1" in term.strip_ansi(out)


# ------------------------------------------------------------------ 分页器


def test_paginate_clamps():
    lines = list("abcdef")
    assert pager.paginate(lines, 4, -3) == 0
    assert pager.paginate(lines, 4, 99) == 2
    assert pager.paginate([], 4, 5) == 0


def test_find_matches_case_insensitive():
    lines = ["Hello", "world", "hello again"]
    assert pager.find_matches(lines, "hello") == [0, 2]
    assert pager.find_matches(lines, "") == []


def test_page_text_fallback_without_color(monkeypatch, capsys):
    monkeypatch.setattr(pager, "USE_COLOR", False)
    pager.page_text("第一行\n第二行", "标题", reader=object())
    out = capsys.readouterr().out
    assert "标题" in out
    assert "第一行" in out and "第二行" in out


def test_pager_interactive_search_and_quit(monkeypatch):
    from collections import deque

    from cli.input import Key

    class _Reader:
        def __init__(self, keys):
            self.keys = deque(keys)

        def get(self, timeout=None):
            return self.keys.popleft()

    import io

    buf = io.StringIO()
    monkeypatch.setattr(pager, "USE_COLOR", True)
    monkeypatch.setattr(pager.sys, "stdout", buf)
    keys = [Key("char", "/")] + [Key("char", ch) for ch in "beta"]
    keys += [Key("enter"), Key("char", "n"), Key("char", "q")]
    text = "\n".join(
        "beta target" if i == 25 else f"line {i}" for i in range(30)
    )
    pager.page_text(text, "分页测试", _Reader(keys))
    out = buf.getvalue()
    assert "\033[?1049h" in out and "\033[?1049l" in out
    assert "找到 1 处" in out
