"""终端原语：ANSI 颜色、显示宽度、裁剪换行、整屏输出。

从 cli_agent.py 拆出（2026-10-08 物理拆分）：只做移动，未改逻辑。"""

from __future__ import annotations

import re
import shutil
import sys
import unicodedata


ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "italic": "\033[3m",
    "underline": "\033[4m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
}


USE_COLOR = bool(sys.stdout.isatty())


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def paint(text: str, color: str = "", bold: bool = False, italic: bool = False) -> str:
    """按需上色；非终端输出时保持纯文本。"""
    if not USE_COLOR:
        return text
    codes = "".join(
        c
        for c in (
            ANSI.get(color),
            ANSI["bold"] if bold else "",
            ANSI["italic"] if italic else "",
        )
        if c
    )
    return f"{codes}{text}{ANSI['reset']}" if codes else text


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def safe_text(text: str) -> str:
    """剔除控制字符，防止模型/工具输出注入终端转义序列。"""
    return "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)


def strwidth(text: str) -> int:
    """终端显示宽度：CJK 全角按 2，ANSI 转义不计。"""
    width = 0
    for ch in strip_ansi(text):
        if unicodedata.east_asian_width(ch) in ("F", "W"):
            width += 2
        elif ch == "\t":
            width += 4 - (width % 4)
        else:
            width += 1
    return width


def clip(text: str, width: int, ellipsis: str = "…") -> str:
    """按显示宽度截断（超宽尾部省略号）。"""
    if strwidth(text) <= width:
        return text
    width -= strwidth(ellipsis)
    out = ""
    w = 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
        if w + cw > width:
            break
        out += ch
        w += cw
    return out + ellipsis


def pad_right(text: str, width: int) -> str:
    return text + " " * max(0, width - strwidth(text))


def wrap_styled(
    text: str, width: int, first_indent: int = 0, indent: int = 0
) -> list[str]:
    """按显示宽度软换行（ANSI 只作零宽处理，CJK 宽字符正确计算）。"""
    if width <= 0:
        width = 40
    lines: list[str] = []
    cur = ""
    cur_w = 0
    limit = width - first_indent
    last_space = -1
    last_space_w = 0
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\x1b":
            m = _ANSI_RE.match(text, i)
            if m:
                cur += m.group(0)
                i = m.end()
                continue
        ch = text[i]
        if ch == "\n":
            lines.append(cur)
            cur = ""
            cur_w = 0
            limit = width - indent
            last_space = -1
            i += 1
            continue
        cw = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
        if ch == " ":
            last_space = len(cur)
            last_space_w = cur_w
        if cur_w + cw > limit:
            if last_space > 0:
                lines.append(cur[:last_space])
                cur = cur[last_space + 1 :] + ch
                cur_w = cur_w - last_space_w - 1 + cw
            else:
                lines.append(cur)
                cur = ch
                cur_w = cw
            limit = width - indent
            last_space = -1
            if ch == " ":
                last_space = len(cur) - 1
                last_space_w = cur_w - 1
        else:
            cur += ch
            cur_w += cw
        i += 1
    if cur:
        lines.append(cur)
    out: list[str] = []
    for idx, line in enumerate(lines):
        pad = " " * (first_indent if idx == 0 else indent)
        out.append(pad + line)
    return out


def term_width(default: int = 80) -> int:
    try:
        return max(40, shutil.get_terminal_size((default, 24)).columns)
    except Exception:
        return default


def visible_rows(text: str, width: int) -> int:
    """文本在终端里占用的行数（含 \\n 与自动折行；CJK 按 2 列、ANSI 不计宽）。

    用于撤回整轮输出时定位块首：光标在块的最后一行（结尾换行时在下一行），
    上移 rows-1 行即到块首。空行也占一行，故 split 后的空串要计入。
    """
    w = max(20, width)
    return sum(max(1, -(-strwidth(line) // w)) for line in text.split("\n"))


class Screen:
    """跟踪当前行状态，避免 spinner/卡片破坏流式文本。"""

    def __init__(self) -> None:
        self.on_line = False
        self.busy = False

    def write(self, text: str = "", end: str = "\n") -> None:
        if self.busy:
            sys.stdout.write("\r\033[2K")
            self.busy = False
        sys.stdout.write(text + end)
        sys.stdout.flush()
        self.on_line = not end.endswith("\n")

    def newline(self) -> None:
        if self.on_line:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.on_line = False

    def busy_line(self, text: str) -> None:
        """在独立行绘制一次性状态（spinner / 状态），可被下次 write 覆盖。"""
        if self.on_line:
            return
        sys.stdout.write("\r\033[2K" + text)
        sys.stdout.flush()
        self.busy = True

    def write_stream(self, text: str) -> None:
        """写入流式文本片段，按内容是否以换行结尾更新行状态。"""
        if not text:
            return
        if self.busy:
            sys.stdout.write("\r\033[2K")
            self.busy = False
        sys.stdout.write(text)
        sys.stdout.flush()
        self.on_line = not text.endswith("\n")

    def erase_block(self, rows: int) -> None:
        """撤回刚打印的一块输出：上移到块首、清除到屏幕末尾。

        rows = 块占用的终端行数（含折行，见 visible_rows）；光标在块的最后
        一行，故上移 rows-1 行后 \r 归零再 ESC[J 清屏。超出可见屏幕的部分
        已进滚动区，无法回收（过渡文本通常只有 1~3 行，不构成问题）。
        """
        if self.busy:
            sys.stdout.write("\r\033[2K")
            self.busy = False
        if rows > 1:
            sys.stdout.write(f"\033[{rows - 1}A")
        sys.stdout.write("\r\033[J")
        sys.stdout.flush()
        self.on_line = False
