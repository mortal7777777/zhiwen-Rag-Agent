"""CLI Markdown 富文本渲染的回归断言（2026-11-19 视觉收口 V5）。

三条都是"实测复现→修复"的确定性缺陷，不是排版偏好：

1. 行内 Markdown 曾经四条正则串行替换，链接正则会在**已着色**的文本上再次
   匹配，把 `\\x1b[1m` 里的 `[1m` 当成链接标签，终端出现 "1m粗体" 残字；
2. 列表只给了续行缩进，首行按整宽排版再拼前缀 → 超出终端宽度（实测 40 列时 45）；
3. 表格列宽只按表头 + 首行数据算，宽值（310ms 对 42ms）被 mid-token 折行。
"""

from __future__ import annotations

import pytest

from cli import format as fmtm
from cli import term


@pytest.fixture(autouse=True)
def _force_color(monkeypatch):
    monkeypatch.setattr(term, "USE_COLOR", True)


def test_inline_md_does_not_swallow_ansi_escapes():
    """粗体 + 链接同行时不得出现被截断的转义序列。"""
    out = fmtm.inline_md("前缀 **粗体字** 与 [链接](https://example.com/x) 结尾")
    for i, ch in enumerate(out):
        if ch == "\x1b" and i + 1 < len(out):
            assert out[i + 1] == "[", f"转义被截断：{out[i:i + 8]!r}"
    assert term.strip_ansi(out) == "前缀 粗体字 与 链接 (https://example.com/x) 结尾"


def test_list_first_line_fits_terminal_width():
    """列表首行（含编号前缀）不超过终端宽度，续行与前缀等宽缩进。"""
    width = 40
    ms = fmtm.MarkdownStream(width)
    out = ms.feed("1. 这是一个很长的编号列表条目，用来观察首行是否超出终端宽度限制\n")
    out += ms.flush()
    lines = [ln for ln in term.strip_ansi(out).split("\n") if ln]
    assert lines, "没有输出"
    for ln in lines:
        assert term.strwidth(ln) <= width, f"超宽 {term.strwidth(ln)} > {width}: {ln!r}"
    assert lines[0].startswith("  1. ")
    assert lines[1].startswith("     ")


def test_table_columns_cover_all_rows():
    """表格列宽必须容纳所有行，不允许把 310ms 拆成两行。"""
    width = 60
    ms = fmtm.MarkdownStream(width)
    md = (
        "| 阶段 | 耗时 | 说明 |\n"
        "| --- | --- | --- |\n"
        "| 召回 | 42ms | 向量 + BM25 |\n"
        "| 重排 | 310ms | 重排模型 |\n"
    )
    out = ms.feed(md) + ms.flush()
    plain = term.strip_ansi(out)
    assert "310ms" in plain, f"宽值被折行：\n{plain}"
    for ln in plain.split("\n"):
        if ln:
            assert term.strwidth(ln) <= width
