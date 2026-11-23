"""终端原语：ANSI 颜色、显示宽度、裁剪换行、整屏输出。

从 cli_agent.py 拆出（2026-10-08 物理拆分）：只做移动，未改逻辑。"""

from __future__ import annotations

import os
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

# 浅色背景终端：基础 8 色对比度不足，用 256 色深色系替代
_LIGHT_CODES = {
    "red": "\033[38;5;160m",
    "green": "\033[38;5;28m",
    "yellow": "\033[38;5;130m",
    "cyan": "\033[38;5;30m",
    "dim": "\033[38;5;245m",
}


def _detect_theme() -> str:
    """ZHIWEN_THEME=dark|light|auto；auto 读取 COLORFGBG 的背景色。"""
    env = (os.environ.get("ZHIWEN_THEME") or "auto").strip().lower()
    if env in ("dark", "light"):
        return env
    fgbg = os.environ.get("COLORFGBG") or ""
    try:
        bg = int(fgbg.split(";")[-1])
        return "light" if bg >= 8 else "dark"
    except ValueError:
        return "dark"


THEME = _detect_theme()


def _detect_no_color() -> bool:
    """NO_COLOR 标准 + ZHIWEN_THEME=none/plain 关闭颜色。"""
    if os.environ.get("NO_COLOR"):
        return True
    return (os.environ.get("ZHIWEN_THEME") or "").strip().lower() in (
        "none", "off", "plain",
    )


USE_COLOR = bool(sys.stdout.isatty()) and not _detect_no_color()

# ASCII 模式：把 UI 装饰字符替换成纯 ASCII（SSH/老终端/无障碍）
ASCII_MODE = (os.environ.get("ZHIWEN_ASCII") or "").strip().lower() in (
    "1", "true", "yes", "on",
)
_ASCII_MAP = {
    "╭": "+", "╮": "+", "╰": "+", "╯": "+", "─": "-", "│": "|",
    "⏺": "*", "⎿": "|", "⏸": "||", "▶": ">", "▸": ">", "•": "-",
    "…": "...", "—": "-", "·": "-", "↑": "^", "↓": "v", "←": "<", "→": ">",
    "💭": "[think]", "📋": "[plan]", "🪝": "[hook]", "👁": "[vision]",
    "🖼": "[image]", "📎": "[file]", "⚠": "!", "✖": "x", "⏹": "[]",
    "⏰": "!", "✔": "ok", "✓": "ok", "○": "o", "█": "#", "░": ".",
    "❓": "?", "▏": "|",
}


def ascii_text(text: str) -> str:
    if not ASCII_MODE:
        return text
    for src, dst in _ASCII_MAP.items():
        text = text.replace(src, dst)
    return text


def _color_code(name: str) -> str:
    if THEME == "light" and name in _LIGHT_CODES:
        return _LIGHT_CODES[name]
    return ANSI.get(name, "")


def _env_on(name: str) -> bool:
    return (os.environ.get(name) or "1").strip().lower() not in (
        "0", "off", "false", "no",
    )


HYPERLINKS = USE_COLOR and _env_on("ZHIWEN_LINKS")
NOTIFY = USE_COLOR and _env_on("ZHIWEN_NOTIFY")


def link(text: str, url: str) -> str:
    """OSC 8 可点击链接；关闭或不支持时退回纯文本。"""
    clean = "".join(ch for ch in str(url or "") if ord(ch) >= 32)
    if not HYPERLINKS or not clean:
        return text
    return f"\033]8;;{clean}\033\\{text}\033]8;;\033\\"


def set_title(text: str) -> None:
    """设置终端标题（生成中 / 等待审批）。"""
    if not USE_COLOR:
        return
    sys.stdout.write(f"\033]0;{ascii_text(str(text))}\007")
    sys.stdout.flush()


def notify(title: str, message: str = "") -> None:
    """桌面通知（OSC 9 + 响铃；Windows Terminal/iTerm2/WezTerm 支持）。"""
    if not NOTIFY:
        return
    body = f"{title}: {message}" if message else title
    sys.stdout.write(f"\033]9;{ascii_text(str(body))}\007\a")
    sys.stdout.flush()


# CSI 颜色 + OSC 8 超链接都按零宽处理（否则换行/裁剪会把链接字节算进宽度）
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;;[^\x1b]*\x1b\\")


def paint(text: str, color: str = "", bold: bool = False, italic: bool = False) -> str:
    """按需上色；非终端输出时保持纯文本。ASCII 模式下同时替换装饰字符。"""
    text = ascii_text(text)
    if not USE_COLOR:
        return text
    codes = "".join(
        c
        for c in (
            _color_code(color),
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


def clip(text: str, width: int, ellipsis: str = "...") -> str:
    """按显示宽度截断（超宽尾部省略号；ANSI/OSC 序列零宽且原样保留）。

    默认用 ASCII 省略号：中文终端常把 U+2026 按 2 列渲染，会让边框错位。
    """
    if strwidth(text) <= width:
        return text
    width -= strwidth(ellipsis)
    out = ""
    w = 0
    i = 0
    opened_link = False
    while i < len(text):
        if text[i] == "\x1b":
            match = _ANSI_RE.match(text, i)
            if match:
                seq = match.group(0)
                out += seq
                if seq.startswith("\x1b]8;;"):
                    # OSC 8：空 URL 是关闭序列，非空是打开序列
                    opened_link = seq != "\x1b]8;;\x1b\\"
                i = match.end()
                continue
        ch = text[i]
        cw = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
        if w + cw > width:
            break
        out += ch
        w += cw
        i += 1
    if opened_link:
        # 截断时在省略号之后补关闭，避免链接状态泄漏到后续输出
        return out + ellipsis + "\x1b]8;;\x1b\\"
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


def term_height(default: int = 24) -> int:
    try:
        return max(8, shutil.get_terminal_size((80, default)).lines)
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
