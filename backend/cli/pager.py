"""长输出分页查看器（alternate screen，类 less 的最小实现）。

Ctrl+O / /output n 打开最近一次工具输出或思考过程；不污染对话滚动区。
非 TTY、无 reader 或 ZHIWEN_PAGER=0 时退回直接打印（最多 200 行）。
"""

from __future__ import annotations

import os
import sys

from cli.term import (
    USE_COLOR,
    clip,
    paint,
    safe_text,
    strip_ansi,
    term_height,
    term_width,
    wrap_styled,
)


def paginate(lines: list[str], page_size: int, top: int) -> int:
    """把滚动位置夹取到合法范围。"""
    page_size = max(1, page_size)
    return max(0, min(top, max(0, len(lines) - page_size)))


def find_matches(lines: list[str], query: str) -> list[int]:
    """返回包含 query 的行号（大小写不敏感，忽略 ANSI）。"""
    needle = (query or "").lower()
    if not needle:
        return []
    return [i for i, line in enumerate(lines) if needle in strip_ansi(line).lower()]


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    for raw in safe_text(text).splitlines():
        lines.extend(wrap_styled(raw, width) or [""])
    return lines


def _print_plain(lines: list[str], title: str) -> None:
    print(paint(title, "cyan", bold=True))
    for line in lines[:200]:
        print(paint(line, "dim"))
    if len(lines) > 200:
        print(paint(f"…（共 {len(lines)} 行，已截断）", "dim"))


def page_text(text: str, title: str, reader=None) -> None:
    """分页显示文本；按键：↑/↓、PgUp/PgDn、g/G、/ 搜索、n/N、q/Esc 返回。"""
    width = term_width()
    lines = _wrap(text, width) or ["（无输出）"]
    if not USE_COLOR or reader is None or os.environ.get("ZHIWEN_PAGER") == "0":
        _print_plain(lines, title)
        return

    height = term_height()
    page_size = max(5, height - 3)
    top = 0
    matches: list[int] = []
    match_idx = -1
    message = ""

    def draw(prompt: str | None = None) -> None:
        sys.stdout.write("\033[H\033[2J")
        sys.stdout.write(paint(clip(title, width), "cyan", bold=True) + "\n")
        sys.stdout.write(paint("─" * width, "dim") + "\n")
        view = lines[top : top + page_size]
        for line in view:
            sys.stdout.write(paint(line, "dim") + "\n")
        for _ in range(page_size - len(view)):
            sys.stdout.write("\n")
        if prompt is None:
            hint = "↑/↓ PgUp/PgDn · g/G 首尾 · / 搜索 · n/N 下一处 · q 返回"
            end = min(top + page_size, len(lines))
            footer = f"{top + 1}-{end}/{len(lines)} · {hint}"
            if message:
                footer = f"{message} · {footer}"
        else:
            footer = prompt
        sys.stdout.write(paint(clip(footer, width), "yellow"))
        sys.stdout.flush()

    def read_query() -> str | None:
        buf = ""
        while True:
            draw(f"/{buf}   回车搜索 · Esc 取消")
            key = reader.get()
            if key is None:
                continue
            if key.name == "enter":
                return buf
            if key.name in ("esc", "ctrl-c"):
                return None
            if key.name == "backspace":
                buf = buf[:-1]
            elif key.name == "char":
                buf += str(key.data)

    sys.stdout.write("\033[?1049h\033[?25l")
    try:
        while True:
            draw()
            key = reader.get()
            if key is None:
                continue
            name = key.name
            if name in ("q", "esc", "ctrl-c") or (
                name == "char" and str(key.data) in ("q", "Q")
            ):
                break
            if name == "up":
                top = paginate(lines, page_size, top - 1)
                message = ""
            elif name == "down":
                top = paginate(lines, page_size, top + 1)
                message = ""
            elif name == "pageup":
                top = paginate(lines, page_size, top - page_size)
                message = ""
            elif name == "pagedown":
                top = paginate(lines, page_size, top + page_size)
                message = ""
            elif name in ("home",) or (name == "char" and key.data == "g"):
                top = 0
                message = ""
            elif name in ("end",) or (name == "char" and key.data == "G"):
                top = paginate(lines, page_size, len(lines))
                message = ""
            elif name == "char" and key.data == "/":
                query = read_query()
                if query is not None:
                    matches = find_matches(lines, query)
                    if matches:
                        match_idx = 0
                        top = paginate(lines, page_size, matches[0] - page_size // 2)
                        message = f"找到 {len(matches)} 处（1/{len(matches)}）"
                    else:
                        match_idx = -1
                        message = f"未找到「{query}」"
            elif name == "char" and key.data in ("n", "N") and matches:
                step = 1 if key.data == "n" else -1
                match_idx = (match_idx + step) % len(matches)
                top = paginate(lines, page_size, matches[match_idx] - page_size // 2)
                message = f"第 {match_idx + 1}/{len(matches)} 处"
    finally:
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
