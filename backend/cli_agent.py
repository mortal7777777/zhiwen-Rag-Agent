"""终端版 Agent 客户端（类 Claude Code CLI 风格，纯标准库，无第三方依赖）。

设计要点（参考 Claude Code 的交互设计）：
- 输入线程与 SSE 读取线程分离，主线程轮询事件队列：
  Ctrl+C / Esc 在生成期间可即时打断（不再被阻塞的 socket 读卡住），
  并调用后端 /api/agent/cancel/{run_id} 让模型立刻收尾；
- 流式 Markdown 渲染：标题 / 列表 / 引用 / 代码块 / 行内代码 / 粗体 / 链接 / 表格；
- 工具调用渲染成卡片（⏺ 调用 … / ⎿ 结果 … 耗时），执行中显示 spinner 与已耗时；
- 权限审批用带边框的数字选项弹窗（1/2/3/4，回车=批准）；
- 状态栏：会话 / 模式 / 模型 / 耗时 / tokens；
- 键盘：↑/↓ 历史，Ctrl+C/Esc 打断，Ctrl+L 清屏，Ctrl+R 重发上一问，
  Ctrl+D 空输入退出，Tab 补全 / 命令，←/→ 光标；
- 历史持久化到目录 .myragagent_history.json，会话到 .myragagent_session.json。

用法（先启动后端）：
    python cli_agent.py
    python cli_agent.py --tool auto --conversation 12
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
import urllib.request

try:
    import colorama

    colorama.just_fix_windows_console()
except Exception:  # pragma: no cover
    pass

# ------------------------------------------------------------------ 基础输出

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


def fmt_dur(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 1.0:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    total = int(seconds)
    m, s = divmod(total, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def fmt_num(n: float) -> str:
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(int(n))


def fmt_tokens(usage: dict) -> str:
    if not usage:
        return ""
    inp = usage.get("input_tokens") or usage.get("prompt_tokens")
    outp = usage.get("output_tokens") or usage.get("completion_tokens")
    parts = []
    if inp is not None:
        parts.append(f"输入 {fmt_num(inp)}")
    if outp is not None:
        parts.append(f"输出 {fmt_num(outp)}")
    return " · ".join(parts)


def term_width(default: int = 80) -> int:
    try:
        return max(40, shutil.get_terminal_size((default, 24)).columns)
    except Exception:
        return default


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


# ------------------------------------------------------------------ Markdown

_MD_HR_RE = re.compile(r"^\s{0,3}([-*_])\1{2,}\s*$")
_MD_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
_MD_LIST_RE = re.compile(r"^(\s{0,4})([-*+]|\d{1,3}[.)])\s+(.*)$")
_MD_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")


def inline_md(text: str) -> str:
    """行内 Markdown：`code`、**bold**、*italic*、[text](url)。"""
    text = re.sub(r"`([^`\n]+)`", lambda m: paint(m.group(1), "cyan"), text)
    text = re.sub(
        r"\*\*([^*\n]+)\*\*", lambda m: paint(m.group(1), bold=True), text
    )
    text = re.sub(
        r"(?<!\*)\*([^*\n]+)\*(?!\*)", lambda m: paint(m.group(1), italic=True), text
    )
    text = re.sub(
        r"\[([^\]\n]+)\]\(([^)\s]+)\)",
        lambda m: paint(m.group(1), "cyan", italic=False)
        + paint(f" ({m.group(2)})", "dim"),
        text,
    )
    return text


class MarkdownStream:
    """流式 Markdown 渲染器：按行渲染，缓冲不完整行。"""

    def __init__(self, width: int) -> None:
        self.width = max(20, width)
        self._line = ""
        self._flushed = 0
        self._in_fence = False
        self._fence_char = ""

    def feed(self, text: str) -> str:
        """输入模型文本，返回本次可以打印的完整内容（含换行）。"""
        out: list[str] = []
        self._line += text
        while "\n" in self._line:
            raw, self._line = self._line.split("\n", 1)
            out.extend(self._complete(raw))
            self._flushed = 0
        return "".join(out)

    def has_partial(self) -> bool:
        return len(self._line) > self._flushed

    def partial(self) -> str:
        """把部分行的未打印片段立即输出（inline 渲染，不换行）。"""
        remaining = self._line[self._flushed :]
        self._flushed = len(self._line)
        if not remaining:
            return ""
        if self._in_fence:
            return paint(remaining, "dim")
        return inline_md(remaining)

    def flush(self) -> str:
        out = self._complete(self._line) if self._line else []
        self._line = ""
        self._flushed = 0
        return "".join(out)

    def _complete(self, raw: str) -> list[str]:
        if self._flushed:
            # 前半段已按部分行打印，只补余下部分
            rest = raw[self._flushed :]
            if self._in_fence:
                m = _MD_FENCE_RE.match(raw)
                if m and m.group(1)[0] == self._fence_char:
                    self._in_fence = False
                    return [paint("└─", "dim") + "\n"]
                return [paint(rest, "dim") + "\n"]
            return [inline_md(rest) + "\n"]
        return self._render(raw)

    def _render(self, raw: str) -> list[str]:
        if self._in_fence:
            m = _MD_FENCE_RE.match(raw)
            if m and m.group(1)[0] == self._fence_char:
                self._in_fence = False
                return [paint("└─", "dim") + "\n"]
            return [paint(raw, "dim") + "\n"]
        m = _MD_FENCE_RE.match(raw)
        if m:
            self._in_fence = True
            self._fence_char = m.group(1)[0]
            label = m.group(2).strip() or "code"
            return [paint(f"┌─ {label}", "dim") + "\n"]
        if not raw.strip():
            return ["\n"]
        h = _MD_HEAD_RE.match(raw)
        if h:
            style = ("yellow", True) if len(h.group(1)) <= 2 else ("cyan", True)
            return [paint(inline_md(h.group(2)), *style) + "\n"]
        if _MD_HR_RE.match(raw):
            return [paint("─" * min(self.width, 60), "dim") + "\n"]
        q = _MD_QUOTE_RE.match(raw)
        if q:
            body = wrap_styled(inline_md(q.group(1)), self.width - 2, indent=2)
            return [paint("│ ", "dim") + line + "\n" for line in body]
        lst = _MD_LIST_RE.match(raw)
        if lst:
            marker = lst.group(2)
            label = "•" if marker in "-*+" else marker.rstrip(".)") + "."
            prefix = "  " + label + " "
            body = wrap_styled(
                inline_md(lst.group(3)), self.width, indent=strwidth(prefix)
            )
            lines = []
            for i, line in enumerate(body):
                pad = prefix if i == 0 else " " * strwidth(prefix)
                lines.append(pad + line + "\n")
            return lines
        if "|" in raw:
            return self._table(raw)
        return [line + "\n" for line in wrap_styled(inline_md(raw), self.width)]

    def _table(self, raw: str) -> list[str]:
        cells = [c.strip() for c in raw.strip().strip("|").split("|")]
        if not cells:
            return [raw + "\n"]
        if all(re.match(r"^:?-{2,}:?$", c) for c in cells):
            return [paint("├" + "─" * min(self.width - 2, 40) + "┤", "dim") + "\n"]
        n = len(cells)
        avail = max(20, self.width - (n * 3 + 1))
        colw = max(6, avail // n)
        rendered = [
            pad_right(" " + clip(inline_md(c), colw) + " ", colw + 2) for c in cells
        ]
        return ["│" + "│".join(rendered) + "│\n"]


# ------------------------------------------------------------------ 输入读取


class Key:
    __slots__ = ("name", "data")

    def __init__(self, name: str, data: object = None) -> None:
        self.name = name
        self.data = data


class KeyReader:
    """后台线程读取键盘，主线程通过队列消费（Ctrl+C 不再被 socket 读阻塞）。"""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._items: collections.deque[Key] = collections.deque()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        """暂停读取（prompt_toolkit 接管 stdin 时调用，避免两个读取者抢键）。"""
        self._paused.set()

    def resume(self) -> None:
        """恢复读取。"""
        self._paused.clear()

    def stop(self) -> None:
        self._stop.set()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def _push(self, key: Key, front: bool = False) -> None:
        with self._cond:
            if front:
                self._items.appendleft(key)
            else:
                self._items.append(key)
            self._cond.notify()

    def drain(self) -> list[Key]:
        with self._cond:
            items = list(self._items)
            self._items.clear()
            return items

    def get(self, timeout: float | None = None) -> Key | None:
        with self._cond:
            self._cond.wait_for(lambda: bool(self._items), timeout)
            if self._items:
                return self._items.popleft()
            return None

    def unget(self, key: Key) -> None:
        self._push(key, front=True)

    def _run(self) -> None:
        if sys.platform == "win32":
            self._run_windows()
        else:
            self._run_unix()

    def _run_windows(self) -> None:
        import msvcrt

        try:
            while not self._stop.is_set():
                if self._paused.is_set():
                    time.sleep(0.02)
                    continue
                try:
                    if not msvcrt.kbhit():
                        time.sleep(0.015)
                        continue
                    ch = msvcrt.getwch()
                except KeyboardInterrupt:
                    # Windows 控制台 Ctrl+C：信号由主线程处理，这里把按键转成
                    # 标准 ctrl-c 事件塞回队列，避免键盘线程被信号打死
                    self._push(Key("ctrl-c"))
                    continue
                if ch in ("\x00", "\xe0"):
                    nxt = msvcrt.getwch()
                    mapping = {
                        "H": "up",
                        "P": "down",
                        "K": "left",
                        "M": "right",
                        "G": "home",
                        "O": "end",
                        "S": "delete",
                    }
                    self._push(Key(mapping.get(nxt, "unknown"), nxt))
                    continue
                self._push_key(ch)
        except Exception:
            # stdin 不是控制台（管道/IDE 终端）时回退到行读取
            while not self._stop.is_set():
                line = sys.stdin.readline()
                if line == "":
                    self._push(Key("ctrl-d"))
                    continue
                for ch in line:
                    self._push_key(ch)

    def _push_key(self, ch: str) -> None:
        if ch == "\x03":
            name = "ctrl-c"
        elif ch == "\x04":
            name = "ctrl-d"
        elif ch == "\x0c":
            name = "ctrl-l"
        elif ch == "\x0f":
            name = "ctrl-o"
        elif ch == "\x12":
            name = "ctrl-r"
        elif ch == "\t":
            name = "tab"
        elif ch in ("\r", "\n"):
            name = "enter"
        elif ch in ("\b", "\x7f"):
            name = "backspace"
        elif ch == "\x1b":
            name = "esc"
        else:
            name = "char"
        self._push(Key(name, ch))

    def _run_unix(self) -> None:
        try:
            import select
            import termios
            import tty

            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                while not self._stop.is_set():
                    if self._paused.is_set():
                        time.sleep(0.02)
                        continue
                    ready, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if not ready:
                        continue
                    data = os.read(fd, 64)
                    self._push_bytes(data)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            while not self._stop.is_set():
                line = sys.stdin.readline()
                if line == "":
                    self._push(Key("ctrl-d"))
                    continue
                for ch in line:
                    self._push_key(ch)

    def _push_bytes(self, data: bytes) -> None:
        if data == b"\x1b":
            try:
                import select

                ready, _, _ = select.select([sys.stdin], [], [], 0.03)
                if ready:
                    data += os.read(sys.stdin.fileno(), 16)
            except Exception:
                pass
        mapping = {
            b"[A": "up",
            b"[B": "down",
            b"[C": "right",
            b"[D": "left",
            b"[H": "home",
            b"[F": "end",
            b"[3~": "delete",
        }
        if data.startswith(b"\x1b[") and data[2:] in mapping:
            self._push(Key(mapping[data[2:]]))
            return
        for byte in data:
            self._push_key(chr(byte))


class Interrupted(Exception):
    """输入态 Ctrl+C/Esc 打断；had_text 表示输入框里已有内容。"""

    def __init__(self, had_text: bool = False) -> None:
        super().__init__()
        self.had_text = had_text


class QuitRequested(Exception):
    pass


class RetryRequested(Exception):
    pass


class OutputRequested(Exception):
    """Ctrl+O：展开最近一次工具调用的完整输出。"""
    pass


COMMANDS = [
    "/exit",
    "/quit",
    "/new",
    "/tools",
    "/todos",
    "/status",
    "/cost",
    "/context",
    "/compact",
    "/rewind",
    "/output",
    "/sandbox",
    "/clear",
    "/init",
    "/resume",
    "/memory",
    "/help",
]


def _common_prefix(strings: list[str]) -> str:
    if not strings:
        return ""
    prefix = strings[0]
    for s in strings[1:]:
        while prefix and not s.startswith(prefix):
            prefix = prefix[:-1]
    return prefix


def input_layout(buf: str, pos: int, cols: int) -> tuple[int, int, int]:
    """计算输入行在终端上的布局：返回 (总行数, 光标所在行, 光标所在列)。

    列宽按显示宽度计算（CJK 全角 2、ANSI 零宽），供渲染时光标定位。
    """
    cols = max(1, cols)
    prompt_w = strwidth("› ")
    total_w = max(1, prompt_w + strwidth(buf))
    rows = (total_w + cols - 1) // cols
    prefix_w = prompt_w + strwidth(buf[:pos])
    pos_row = prefix_w // cols
    pos_col = prefix_w % cols
    return rows, pos_row, pos_col


def complete(buf: str) -> tuple[list[str], str]:
    """返回 (候选列表, 幽灵补全文本)。"""
    if " " in buf and buf.split(" ", 1)[0] == "/tools":
        _, _, arg = buf.partition(" ")
        values = ["auto", "knowledge", "web", "none"]
        matches = [v for v in values if v.startswith(arg) and v != arg]
        cp = _common_prefix(matches)
        return matches, cp[len(arg):] if cp else ""
    if " " in buf and buf.split(" ", 1)[0] == "/sandbox":
        _, _, arg = buf.partition(" ")
        values = ["docker", "subprocess", "off"]
        matches = [v for v in values if v.startswith(arg) and v != arg]
        cp = _common_prefix(matches)
        return matches, cp[len(arg):] if cp else ""
    if buf.startswith("/"):
        word = buf.split(" ", 1)[0]
        matches = [c for c in COMMANDS if c.startswith(word)]
        cp = _common_prefix(matches)
        ghost = cp[len(word) :] if len(cp) > len(word) else ""
        return matches, ghost
    return [], ""


_INPUT_ROWS = [1]  # 上次输入块占用的终端行数（含菜单行），用于回清残留


def _render_input(buf: str, pos: int, with_ghost: bool = True) -> None:
    """重绘输入行；支持跨行内容（回清上一次占用的所有行，避免残影堆叠）。"""
    if not USE_COLOR:
        return  # 非终端（管道输入）不重绘，避免转义序列污染输出
    matches, ghost = complete(buf)
    prompt = paint("› ", "green", bold=True)
    ghost_text = paint(ghost, "dim") if with_ghost and ghost else ""
    line = prompt + buf + ghost_text
    cols = term_width()
    rows, pos_row, pos_col = input_layout(
        buf + (ghost if with_ghost else ""), pos, cols
    )
    old_rows = _INPUT_ROWS[0]

    sys.stdout.write("\033[?25l")  # 隐藏光标，减少闪烁
    # 1) 光标回到上次输入块顶部，并清掉所有旧行
    if old_rows > 1:
        sys.stdout.write(f"\033[{old_rows - 1}A")
    sys.stdout.write("\r\033[2K")
    # 2) 写出新内容（终端按 cols 自动换行）
    sys.stdout.write(line)
    # 3) 新块行数变少时，向下清残留行
    extra = max(0, old_rows - rows)
    for _ in range(extra):
        sys.stdout.write("\033[1B\r\033[2K")
    # 4) 从行尾回到块顶，再定位到 pos
    up = (rows - 1) + extra
    if up:
        sys.stdout.write(f"\033[{up}A")
    sys.stdout.write("\r")
    if pos_row:
        sys.stdout.write(f"\033[{pos_row}B")
    if pos_col:
        sys.stdout.write(f"\033[{pos_col + 1}G")
    # 5) / 命令候选菜单（画在输入块下方一行，再回到光标处）
    menu_rows = 0
    if buf.startswith("/") and matches and with_ghost:
        down = rows - pos_row
        if down:
            sys.stdout.write(f"\033[{down}B")
        sys.stdout.write("\r\033[2K")
        menu = "  " + "  ".join(clip(m, 26) for m in matches[:6])
        sys.stdout.write(clip(menu, cols))
        menu_rows = 1
        if down:
            sys.stdout.write(f"\033[{down}A")
        sys.stdout.write("\r")
        if pos_col:
            sys.stdout.write(f"\033[{pos_col + 1}G")
    _INPUT_ROWS[0] = rows + menu_rows
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()


def _toolbar_text():
    """底部工具栏：常驻快捷键提示；? 展开完整命令面板（类 Claude Code）。"""
    key = lambda s: ("class:tbkey", s)
    txt = lambda s: ("class:tb", s)
    if not HELP_OPEN["on"]:
        return [
            key(" ? "),
            txt("命令帮助   "),
            key(" / "),
            txt("命令补全   "),
            key("Ctrl+O "),
            txt("展开输出   "),
            key("Ctrl+R "),
            txt("重发   "),
            key("Ctrl+C "),
            txt("打断 / 双击退出"),
        ]
    return [
        key(" /context "), txt("上下文占用   "),
        key(" /compact "), txt("手动压缩   "),
        key(" /rewind "), txt("消息回退   "),
        key(" /output [n] "), txt("展开工具输出\n"),
        key(" /todos "), txt("任务清单   "),
        key(" /cost "), txt("用量   "),
        key(" /status "), txt("状态   "),
        key(" /tools auto|knowledge|web|none\n"),
        key(" /new "), txt("新会话   "),
        key(" /resume "), txt("恢复会话   "),
        key(" /init "), txt("创建 AGENTS.md   "),
        key(" /memory "), txt("项目记忆\n"),
        key(" /clear "), txt("清屏   "),
        key(" /exit "), txt("退出   "),
        key(" ? "), txt("收起面板"),
    ]


def make_prompt_session(history: list[str]):
    """构建 prompt_toolkit PromptSession：历史 + Tab 补全 + 键绑定语义。

    取代手写 KeyReader 行编辑（Windows 终端上末字符不可见/光标错位/
    Ctrl+C 竞争等 bug），与 Hermes CLI 同款方案。
    底部工具栏常驻快捷键提示，空输入按 ? 展开/收起完整命令面板。
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings

    class _CmdCompleter(Completer):
        def get_completions(self, document, complete_event):
            matches, _ = complete(document.text)
            word = document.get_word_before_cursor()
            for m in matches:
                yield Completion(m, start_position=-len(word))

    kb = KeyBindings()

    @kb.add("escape")
    def _(event):
        buf = event.app.current_buffer
        if buf.text:
            # Esc：有内容 = 清空输入（与旧行为一致）
            buf.text = ""
            buf.cursor_position = 0
        elif HELP_OPEN["on"]:
            # Esc：空输入且面板开着 = 收起帮助面板
            HELP_OPEN["on"] = False
            event.app.invalidate()

    @kb.add("c-d")
    def _(event):
        if not event.app.current_buffer.text:
            raise QuitRequested()

    @kb.add("c-r")
    def _(event):
        raise RetryRequested()

    @kb.add("c-o")
    def _(event):
        raise OutputRequested()

    @kb.add("?")
    def _(event):
        buf = event.app.current_buffer
        if buf.text:
            buf.insert_text("?")
        else:
            # 空输入按 ?：展开/收起命令帮助面板（Claude Code 式）
            HELP_OPEN["on"] = not HELP_OPEN["on"]
            event.app.invalidate()

    pt_history = InMemoryHistory()
    for h in history[-200:]:
        pt_history.append_string(h)

    return PromptSession(
        history=pt_history,
        completer=_CmdCompleter(),
        key_bindings=kb,
        enable_history_search=True,
        bottom_toolbar=_toolbar_text,
    )


def read_line(reader: KeyReader, history: list[str], default: str = "", status: str = "") -> str:
    """prompt_toolkit 行编辑；Ctrl+C/Esc/D/R/O 抛对应异常（与旧语义兼容）。

    default：预填文本（/rewind 后把被回退的用户消息放回输入框）。
    status：输入区状态行（模型/工具/会话/上下文），空串则只显示提示符；
    与对话历史之间用全宽分隔线隔开（类 Claude Code 输入框分区）。
    """
    # 非 TTY（管道/IDE 终端/测试）走 legacy：prompt_toolkit 需要真实终端，
    # 管道输入下会挂起等待
    if not sys.stdin.isatty():
        return _read_line_legacy(reader, history, default)
    try:
        session = make_prompt_session(history)
    except Exception:
        # prompt_toolkit 不可用（极老环境/非 Windows 控制台）时回退旧实现
        return _read_line_legacy(reader, history, default, status)
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.styles import Style

    # prompt 消息是多行富文本：分隔线 + 状态行 + 提示符
    # （prompt_toolkit 的 message 不能带 ANSI 转义串，颜色走样式类）
    message = FormattedText([("class:prompt", "› ")])
    if status:
        cols = term_width()
        message = FormattedText(
            [
                ("class:status", "─" * cols + "\n"),
                ("class:status", " " + status + "\n"),
                ("class:prompt", "› "),
            ]
        )

    # prompt_toolkit 接管 stdin 期间暂停 KeyReader，避免两个读取者抢键
    reader.pause()
    try:
        text = session.prompt(
            message,
            style=Style.from_dict(
                {
                    "prompt": "ansigreen bold",
                    "status": "ansibrightblack",
                    "bottom-toolbar": "noreverse bg:#262626 fg:#9e9e9e",
                    ".tbkey": "fg:ansicyan bold",
                    ".tb": "fg:#c8c8c8",
                }
            ),
            multiline=False,
            wrap_lines=True,
            default=default or "",
        )
    except KeyboardInterrupt:
        # 空输入 Ctrl+C：转 Interrupted(False)（双击退出由主循环处理）；
        # 有输入时 prompt_toolkit 默认清空并继续，不抛异常
        raise Interrupted(False)
    except EOFError:
        raise QuitRequested()
    finally:
        reader.resume()
        HELP_OPEN["on"] = False  # 提交后收起帮助面板
    if text and (not history or history[-1] != text):
        history.append(text)
        del history[:-200]
    return text


def _read_line_legacy(
    reader: KeyReader, history: list[str], default: str = "", status: str = ""
) -> str:
    """旧实现：仅当 prompt_toolkit 不可用/非 TTY 时回退（保持 Ctrl+C 语义）。"""
    if not sys.stdin.isatty():
        # 管道/IDE 输入：直接行读取（msvcrt 在非 TTY 下 kbhit 恒 False 会死等）
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            raise Interrupted(False)
        line = line.rstrip("\r\n")
        if line and (not history or history[-1] != line):
            history.append(line)
            del history[:-200]
        return line
    buf = default
    pos = len(buf)
    draft = ""
    idx = len(history)
    if status:
        sys.stdout.write(paint("─" * term_width(), "dim") + "\n")
        sys.stdout.write(paint(f" {status}", "dim") + "\n")
    _INPUT_ROWS[0] = 1
    sys.stdout.write(paint("› ", "green", bold=True))
    sys.stdout.flush()
    if buf:
        _render_input(buf, pos)
    while True:
        try:
            key = reader.get()
        except KeyboardInterrupt:  # Windows 控制台 Ctrl+C 走信号路径
            raise Interrupted(bool(buf))
        if key is None:
            continue
        try:
            if key.name == "char":
                buf = buf[:pos] + str(key.data) + buf[pos:]
                pos += 1
            elif key.name == "backspace":
                if pos > 0:
                    buf = buf[: pos - 1] + buf[pos:]
                    pos -= 1
            elif key.name == "delete":
                if pos < len(buf):
                    buf = buf[:pos] + buf[pos + 1 :]
            elif key.name == "left":
                pos = max(0, pos - 1)
            elif key.name == "right":
                pos = min(len(buf), pos + 1)
            elif key.name == "home":
                pos = 0
            elif key.name == "end":
                pos = len(buf)
            elif key.name == "up":
                if history and idx > 0:
                    if idx == len(history):
                        draft = buf
                    idx -= 1
                    buf = history[idx]
                    pos = len(buf)
            elif key.name == "down":
                if idx < len(history):
                    idx += 1
                    if idx == len(history):
                        buf, draft = draft, ""
                    else:
                        buf = history[idx]
                    pos = len(buf)
            elif key.name == "tab":
                _, ghost = complete(buf)
                if ghost:
                    buf += ghost
                    pos = len(buf)
            elif key.name == "esc":
                if buf:
                    raise Interrupted(True)
            elif key.name == "ctrl-c":
                raise Interrupted(bool(buf))
            elif key.name == "ctrl-d":
                if not buf:
                    raise QuitRequested()
            elif key.name == "ctrl-l":
                sys.stdout.write("\033[2J\033[H")
                _INPUT_ROWS[0] = 1
            elif key.name == "ctrl-o":
                raise OutputRequested()
            elif key.name == "ctrl-r":
                raise RetryRequested()
            elif key.name == "enter":
                _render_input(buf, pos, with_ghost=False)
                sys.stdout.write("\n")
                sys.stdout.flush()
                _INPUT_ROWS[0] = 1
                if buf:
                    if not history or history[-1] != buf:
                        history.append(buf)
                        del history[:-200]
                return buf
            else:
                continue
            _render_input(buf, pos)
        except KeyboardInterrupt:  # Windows 控制台 Ctrl+C 走信号路径
            raise Interrupted(bool(buf))


def _clear_screen() -> None:
    if USE_COLOR:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    _INPUT_ROWS[0] = 1


# ------------------------------------------------------------------ 会话/历史

SESSION_FILE = ".myragagent_session.json"
HISTORY_FILE = ".myragagent_history.json"


def save_session(cwd: str, conversation_id: int) -> None:
    """按目录记录最近会话（最多 10 条，最近在前）。"""
    path = os.path.join(cwd, SESSION_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    history = [
        h
        for h in (data.get("history") or [])
        if h.get("conversation_id") != conversation_id
    ]
    history.insert(0, {"conversation_id": conversation_id, "updated_at": time.time()})
    data["history"] = history[:10]
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def load_session_history(cwd: str) -> list[dict]:
    try:
        with open(os.path.join(cwd, SESSION_FILE), encoding="utf-8") as f:
            return (json.load(f) or {}).get("history") or []
    except Exception:
        return []


def load_history(cwd: str) -> list[str]:
    try:
        with open(os.path.join(cwd, HISTORY_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return [str(x) for x in data if isinstance(x, str)][-200:]
    except Exception:
        return []


def save_history(cwd: str, history: list[str]) -> None:
    try:
        with open(os.path.join(cwd, HISTORY_FILE), "w", encoding="utf-8") as f:
            json.dump(history[-200:], f, ensure_ascii=False)
    except Exception:
        pass


def fetch_conversation_titles(base_url: str) -> dict[int, str]:
    try:
        with urllib.request.urlopen(f"{base_url}/api/conversations", timeout=10) as resp:
            items = json.loads(resp.read().decode("utf-8"))
        return {int(c.get("id")): str(c.get("title") or "新对话") for c in items}
    except Exception:
        return {}


def fetch_conversation_messages(base_url: str, conversation_id: int, limit: int = 6) -> list[dict]:
    """拉取会话最近的若干条消息（用于 /resume 后展示上下文）。"""
    try:
        url = f"{base_url}/api/conversations/{conversation_id}/messages"
        with urllib.request.urlopen(url, timeout=10) as resp:
            items = json.loads(resp.read().decode("utf-8"))
        if not isinstance(items, list):
            return []
        # 只取最近的 N 条（消息按 id 升序返回）
        return items[-limit:]
    except Exception:
        return []


def fetch_model_label(base_url: str) -> str | None:
    try:
        with urllib.request.urlopen(f"{base_url}/api/settings", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        active = data.get("active_chat_provider") or ""
        for p in data.get("providers") or []:
            if p.get("id") == active:
                return p.get("model") or active
        return active or None
    except Exception:
        return None


def fetch_todos(base_url: str, conversation_id: int | None) -> None:
    if not conversation_id:
        print(paint("还没有会话，先提问。", "yellow"))
        return
    try:
        with urllib.request.urlopen(
            f"{base_url}/api/todos/{conversation_id}", timeout=10
        ) as resp:
            todos = json.loads(resp.read().decode("utf-8")).get("todos") or []
    except Exception as exc:
        print(paint(f"读取任务清单失败：{exc}", "red"))
        return
    if not todos:
        print(paint("当前没有任务清单。", "dim"))
        return
    print(paint("任务清单", "cyan", bold=True))
    for t in todos:
        mark = "[x]" if t.get("done") else "[ ]"
        color = "green" if t.get("done") else "dim"
        print(paint(f"  {mark} {safe_text(t.get('text', ''))}", color))


def fetch_usage(base_url: str, conversation_id: int | None, limit: int = 1) -> dict:
    if not conversation_id:
        return {}
    try:
        url = f"{base_url}/api/runs?conversation_id={conversation_id}&limit={limit}"
        with urllib.request.urlopen(url, timeout=4) as resp:
            runs = json.loads(resp.read().decode("utf-8")) or []
        usage: dict = {}
        for run in runs:
            u = run.get("token_usage") or {}
            for key in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens"):
                usage[key] = int(usage.get(key) or 0) + int(u.get(key) or 0)
        return usage
    except Exception:
        return {}


def show_cost(base_url: str, conversation_id: int | None) -> None:
    if not conversation_id:
        print(paint("还没有会话，先提问。", "yellow"))
        return
    usage = fetch_usage(base_url, conversation_id, limit=200)
    if not usage:
        print(paint("暂无用量记录。", "dim"))
        return
    text = fmt_tokens(usage) or "暂无"
    print(paint(f"本会话 tokens：{text}", "dim"))


def http_json(base_url: str, method: str, path: str, body: dict | None = None, timeout: int = 120) -> dict | list:
    """通用 JSON 请求辅助（/context /compact /rewind 等新命令共用）。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data if method == "POST" else None,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------- 输入区状态（模型/上下文 meter，Claude Code 式底栏） ----------------

# 最近一次上下文统计：done 事件后与 /context /compact /rewind 命令后刷新
LAST_CONTEXT: dict = {"stats": None}

# ? 键展开的命令帮助面板开关（prompt_toolkit 底部工具栏用）
HELP_OPEN: dict = {"on": False}


def fetch_context_stats(base_url: str, conversation_id: int | None) -> dict | None:
    """拉取上下文占用统计；失败静默（状态行缺上下文不影响使用）。"""
    if not conversation_id:
        return None
    try:
        stats = http_json(base_url, "GET", f"/api/agent/context/{conversation_id}", timeout=8)
        LAST_CONTEXT["stats"] = stats
        return stats
    except Exception:
        return None


def context_pct(stats: dict | None) -> int:
    if not stats:
        return 0
    try:
        tokens = int(stats.get("estimated_tokens") or 0)
        budget = int(stats.get("token_budget") or 0)
        return min(100, round(100 * tokens / budget)) if budget else 0
    except Exception:
        return 0


def ctx_meter(stats: dict | None, width: int = 10) -> str:
    """紧凑上下文表：上下文 [██░░░░░░░░] 12%（token 占预算比例）。"""
    if not stats:
        return ""
    pct = context_pct(stats)
    filled = int(width * pct / 100)
    return f"上下文 {'█' * filled}{'░' * (width - filled)} {pct}%"


def build_status_line(
    model: str | None,
    tool_mode: str,
    conversation_id: int | None,
    sandbox: str | None = None,
) -> str:
    """输入框上方的状态行：模型 · 工具模式 · 会话 · 沙箱 · 上下文 meter。"""
    bits = [f"模型 {model or '未知'}", f"工具 {tool_mode}"]
    bits.append(f"会话 {conversation_id}" if conversation_id else "会话 新")
    if sandbox:
        bits.append(f"沙箱 {sandbox}")
    meter = ctx_meter(LAST_CONTEXT.get("stats"))
    if meter:
        bits.append(meter)
    return " · ".join(bits)


# 本会话的工具完整输出（供 Ctrl+O / /output 展开查看）：最新在前访问
TOOL_OUTPUTS: collections.deque = collections.deque(maxlen=30)


def record_tool_output(name: str, summary: str, detail: str) -> None:
    TOOL_OUTPUTS.append({"name": name, "summary": summary, "detail": detail or summary or ""})


def show_tool_output(n: int = 1) -> None:
    """展开最近第 n 次（默认 1）工具调用的完整输出。"""
    if not TOOL_OUTPUTS:
        print(paint("本会话还没有工具输出。", "yellow"))
        return
    if not 1 <= n <= len(TOOL_OUTPUTS):
        print(paint(f"只有 {len(TOOL_OUTPUTS)} 条工具输出，序号需在 1~{len(TOOL_OUTPUTS)} 之间。", "yellow"))
        return
    item = list(TOOL_OUTPUTS)[-n]
    idx = len(TOOL_OUTPUTS) - n + 1
    print(
        paint(f"⏺ {item['name']}（第 {idx}/{len(TOOL_OUTPUTS)} 条）", "cyan", bold=True)
        + paint(f"  {clip(item['summary'] or '', term_width() - 20)}", "dim")
    )
    width = term_width()
    for line in item["detail"].splitlines()[:200]:
        print(paint(clip(safe_text(line), width), "dim"))
    total = len(item["detail"].splitlines())
    if total > 200:
        print(paint(f"…（共 {total} 行，已截断）", "dim"))


def show_context(base_url: str, conversation_id: int | None) -> None:
    """上下文占用统计：消息条数 / token 预算 / 滚动摘要进度。"""
    if not conversation_id:
        print(paint("还没有会话，先提问。", "yellow"))
        return
    stats = fetch_context_stats(base_url, conversation_id)
    if stats is None:
        print(paint("读取上下文统计失败（后端未启动或接口异常）。", "red"))
        return

    def _bar(cur: int, cap: int, width: int = 24) -> str:
        filled = int(width * min(1.0, cur / max(1, cap)))
        return "█" * filled + "░" * (width - filled)

    count = int(stats.get("message_count") or 0)
    max_msgs = int(stats.get("history_max_messages") or 1)
    tokens = int(stats.get("estimated_tokens") or 0)
    budget = int(stats.get("token_budget") or 1)
    print(paint("上下文占用", "cyan", bold=True))
    print(paint(f"   消息   {_bar(count, max_msgs)} {count}/{max_msgs} 条", "dim"))
    print(paint(f"   tokens {_bar(tokens, budget)} {fmt_num(tokens)}/{fmt_num(budget)}（估算）", "dim"))
    if stats.get("summary_chars"):
        print(
            paint(
                f"   摘要   {stats['summary_chars']} 字 · 已覆盖 "
                f"{stats.get('summary_covered_messages', 0)} 条早期消息",
                "dim",
            )
        )
    else:
        print(paint("   摘要   未生成", "dim"))
    recent_runs = int(stats.get("recent_runs") or 0)
    if recent_runs:
        rate = stats.get("recent_cache_hit_rate")
        rate_txt = f"{rate * 100:.0f}%" if rate is not None else "-"
        print(
            paint(
                f"   缓存   {rate_txt} 命中率（近 {recent_runs} 次运行 · "
                f"主循环 {stats.get('recent_llm_calls', 0)} 次调用）",
                "dim",
            )
        )
    if stats.get("compaction_would_trigger"):
        print(paint("   超出软窗口，下一轮将自动压缩（也可 /compact 立即压缩）。", "yellow"))
    else:
        print(paint("   未超预算，暂不需要压缩。", "dim"))


def run_compact(base_url: str, conversation_id: int | None) -> None:
    """手动压缩：保留近期消息，更早的并入滚动摘要。"""
    if not conversation_id:
        print(paint("还没有会话，先提问。", "yellow"))
        return
    print(paint("压缩中…（需要一次模型调用，稍等）", "dim"))
    try:
        result = http_json(base_url, "POST", f"/api/agent/compact/{conversation_id}", timeout=180)
    except Exception as exc:
        print(paint(f"压缩失败：{exc}", "red"))
        return
    fetch_context_stats(base_url, conversation_id)  # 摘要进度变了，meter 同步
    if result.get("compacted"):
        print(
            paint(
                f"✔ 已压缩：保留最近 {result.get('kept_messages')} 条，"
                f"{result.get('summarized_messages')} 条并入摘要"
                f"（{result.get('summary_chars')} 字）。",
                "green",
            )
        )
    else:
        print(paint(f"未压缩：{result.get('reason') or '无需压缩'}", "yellow"))


def run_rewind(base_url: str, conversation_id: int | None, reader: KeyReader, arg: str) -> str | None:
    """消息级回退：选一条历史用户消息，删除其后全部对话并放回输入框。

    返回预填文本（被回退消息的内容），None 表示未执行。
    """
    if not conversation_id:
        print(paint("还没有会话，先提问。", "yellow"))
        return None
    try:
        msgs = http_json(
            base_url, "GET", f"/api/conversations/{conversation_id}/messages", timeout=15
        )
    except Exception as exc:
        print(paint(f"读取会话消息失败：{exc}", "red"))
        return None
    if not isinstance(msgs, list):
        msgs = []
    user_msgs = [m for m in msgs if m.get("role") == "user"][-8:]
    if not user_msgs:
        print(paint("本会话还没有可回退的用户消息。", "yellow"))
        return None
    index = -1
    if arg.isdigit() and 1 <= int(arg) <= len(user_msgs):
        index = int(arg) - 1
    else:
        print(paint("最近的用户消息（输入序号回退，回车取消）：", "cyan", bold=True))
        for i, m in enumerate(user_msgs, 1):
            print(paint(f"  {i}. {clip(safe_text(m.get('content') or ''), term_width() - 6)}", "dim"))
        try:
            sel = read_line(reader, []).strip()
        except (Interrupted, QuitRequested):
            sel = ""
        except OutputRequested:
            sel = ""
        index = int(sel) - 1 if sel.isdigit() and 1 <= int(sel) <= len(user_msgs) else -1
    if index < 0:
        print(paint("已取消回退。", "dim"))
        return None
    target = user_msgs[index]
    try:
        result = http_json(
            base_url,
            "POST",
            f"/api/conversations/{conversation_id}/rewind",
            {"message_id": target.get("id")},
            timeout=30,
        )
    except Exception as exc:
        print(paint(f"回退失败：{exc}", "red"))
        return None
    removed = result.get("removed")
    extra = "，滚动摘要已重置" if result.get("summary_reset") else ""
    print(paint(f"✔ 已回退：删除 {removed} 条消息{extra}。原消息已放回输入框，可直接编辑重发。", "green"))
    return str(target.get("content") or "")


def init_project_memory(cwd: str) -> None:
    path = os.path.join(cwd, "AGENTS.md")
    if os.path.exists(path):
        print(paint("AGENTS.md 已存在，未覆盖。", "yellow"))
        return
    template = (
        "# AGENTS.md\n\n"
        "> 本文件由 AI 助手自动加载；按需维护。\n\n"
        "## 项目背景\n\n"
        "（描述这个目录里的项目是做什么的）\n\n"
        "## 约定\n\n"
        "- 描述代码风格、目录结构、常用命令等约定\n\n"
        "## 注意事项\n\n"
        "- 描述需要注意的坑与约束\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(template)
    print(paint(f"已创建 {path}，下一轮对话会自动加载。", "green"))


def show_project_memory(cwd: str) -> None:
    """展示后端会合并加载的 AGENTS.md 链（当前目录逐级向上 + 用户级）。"""
    paths: list[str] = []
    seen: set[str] = set()
    d = os.path.abspath(cwd)
    while True:
        p = os.path.join(d, "AGENTS.md")
        if os.path.exists(p) and p not in seen:
            seen.add(p)
            paths.append(p)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    user = os.path.join(os.path.expanduser("~"), ".myragagent", "AGENTS.md")
    if os.path.exists(user) and user not in seen:
        paths.append(user)
    if not paths:
        print(paint("未找到任何 AGENTS.md（可用 /init 在当前目录创建）。", "yellow"))
        return
    for p in paths:
        print(paint(f"── {p}", "cyan", bold=True))
        try:
            with open(p, encoding="utf-8") as f:
                content = f.read()
        except Exception as exc:
            print(paint(f"  读取失败：{exc}", "red"))
            continue
        for line in content.splitlines()[:40]:
            print(paint(clip(line, term_width() - 2), "dim"))


# ------------------------------------------------------------------ 权限审批


def resolve_permission(
    base_url: str,
    request_id: str,
    approve: bool,
    reason: str = "",
    remember_forever: bool = False,
    remember_session: bool = False,
) -> bool:
    body = json.dumps(
        {
            "approve": approve,
            "reason": reason,
            "remember": remember_forever,
            "remember_session": remember_session,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/agent/permission/{request_id}/resolve",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200
    except Exception:
        return False


def _boxed(rows: list[str], title: str, color: str) -> None:
    inner = max([strwidth(r) for r in rows] + [strwidth(title) + 4] + [0])
    width = min(term_width() - 2, inner + 2)
    print(paint("┌─ " + title + " " + "─" * max(0, width - strwidth(title) - 4) + "┐", color, bold=True))
    for row in rows:
        print(paint("│ " + row + " " * max(0, width - strwidth(row) - 1) + "│", color))
    print(paint("└" + "─" * max(0, width - 1) + "┘", color, bold=True))


def _permission_rows(data: dict) -> list[str]:
    """按工具类型构造审批弹窗内容：edit_file 渲染行级 diff（红删绿增），
    write_file 渲染内容预览，其余原样展示参数。"""
    args = data.get("arguments") or {}
    if not isinstance(args, dict):
        args = {}
    name = data.get("name")
    rows: list[str] = []
    width = term_width()
    if name == "edit_file" and args.get("diff_lines"):
        rows.append(clip(f"文件：{safe_text(args.get('path') or '')}", width - 6))
        if args.get("replace_all"):
            rows.append(paint("替换全部匹配处", "yellow"))
        rows.append(paint("──── 变更预览 ────", "dim"))
        for d in args["diff_lines"][:60]:
            op = str(d.get("op") or "")
            text = clip(safe_text(str(d.get("text") or "")), width - 6)
            if op == "-":
                rows.append(paint(f"− {text}", "red"))
            elif op == "+":
                rows.append(paint(f"+ {text}", "green"))
            else:
                rows.append(paint(f"  {text}", "dim"))
        if len(args["diff_lines"]) > 60:
            rows.append(paint(f"  …（共 {len(args['diff_lines'])} 行 diff，已截断）", "dim"))
        return rows
    if name == "write_file":
        rows.append(clip(f"文件：{safe_text(args.get('path') or '')}", width - 6))
        preview = str(args.get("content_preview") or "")
        if preview:
            rows.append(paint("──── 内容预览 ────", "dim"))
            for line in preview.splitlines()[:12]:
                rows.append(paint(clip(safe_text(line), width - 6), "dim"))
            if len(preview.splitlines()) > 12:
                rows.append(paint("  …（预览截断）", "dim"))
        return rows
    # 其余工具（bash 等）：逐参数展示
    for k, v in args.items():
        rows.append(clip(f"{k}: {safe_text(json.dumps(v, ensure_ascii=False))}", width - 6))
    return rows


def prompt_permission(reader: KeyReader, data: dict, base_url: str) -> None:
    """渲染审批弹窗并等待数字键决定（回车=批准，Esc=拒绝）。"""
    print()
    rows = _permission_rows(data)
    is_command = data.get("name") in ("bash", "command_tool")
    opts = ["1) 批准", "2) 拒绝"]
    if is_command:
        opts += ["3) 批准(本会话)", "4) 批准(永久)"]
    rows += [paint("  ".join(opts), "cyan")]
    _boxed(rows, safe_text(data.get("summary", "等待人工确认")), "yellow")
    print(paint("选择 [1-4] / 回车=批准 / Esc=拒绝：", "yellow"), end="", flush=True)
    while True:
        key = reader.get(timeout=300)
        if key is None:
            answer = "2"
        elif key.name == "enter":
            answer = "1"
        elif key.name == "esc":
            answer = "2"
        elif key.name == "char" and key.data in ("1", "2", "3", "4"):
            answer = str(key.data)
        else:
            continue
        break  # 数字键/回车/Esc 立即生效，不再等待 reason 输入
    print()
    approve = answer in ("1", "3", "4")
    remember_session = answer == "3" and is_command
    remember_forever = answer == "4" and is_command
    if approve:
        ok = resolve_permission(
            base_url,
            data.get("id", ""),
            True,
            "",
            remember_forever=remember_forever,
            remember_session=remember_session,
        )
        print(paint("✔ 已批准，继续执行…", "green") if ok else paint("✘ 提交失败，请检查后端", "red"))
        return
    ok = resolve_permission(base_url, data.get("id", ""), False, "")
    print(paint("✘ 已拒绝", "red") if ok else paint("✘ 提交失败，请检查后端", "red"))
    return


# ------------------------------------------------------------------ 流式问答

SPINNER = "|/-\\"


class GenerationInterrupted(Exception):
    pass


def render_tool_args(arguments: object) -> str:
    if not arguments:
        return ""
    if isinstance(arguments, str):
        return clip(safe_text(arguments), 100)
    try:
        parts = []
        for k, v in dict(arguments).items():
            parts.append(f"{k}={clip(safe_text(json.dumps(v, ensure_ascii=False)), 40)}")
        return clip(", ".join(parts), 100)
    except Exception:
        return clip(safe_text(str(arguments)), 100)


def _cancel_run(base_url: str, run_id: str | None) -> bool:
    if not run_id:
        return False
    req = urllib.request.Request(
        f"{base_url}/api/agent/cancel/{run_id}", data=b"", method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def stream_question(
    base_url: str,
    question: str,
    tool_mode: str,
    conversation_id: int | None,
    reader: KeyReader,
    screen: Screen,
    sandbox: str | None = None,
) -> int:
    """发送问题并渲染 SSE 事件；Ctrl+C/Esc 即时打断并通知后端取消。"""
    body = json.dumps(
        {
            "question": question,
            "conversation_id": conversation_id,
            "tool_mode": tool_mode,
            "template_id": None,
            "project_dir": os.getcwd(),
            "command_sandbox": sandbox,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/agent/stream",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    lines_q: collections.deque = collections.deque()
    lines_cond = threading.Condition()
    resp_holder: list = []
    done_flag = threading.Event()

    def reader_thread() -> None:
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                resp_holder.append(resp)
                for raw in resp:
                    if done_flag.is_set():
                        break
                    with lines_cond:
                        lines_q.append(raw.decode("utf-8", errors="replace"))
                        lines_cond.notify()
        except Exception as exc:
            with lines_cond:
                lines_q.append(("__error__", str(exc)))
                lines_cond.notify()
        finally:
            with lines_cond:
                lines_q.append(("__end__", None))
                lines_cond.notify()

    threading.Thread(target=reader_thread, daemon=True).start()

    md = MarkdownStream(term_width())
    run_id: str | None = None
    conv_id: int | None = conversation_id
    t0 = time.time()
    active_tools: dict[str, tuple[str, float]] = {}
    busy_text: str | None = None
    interrupted = False
    frame = 0
    last_partial = time.time()

    def _next_item():
        with lines_cond:
            lines_cond.wait_for(lambda: bool(lines_q), 0.12)
            if lines_q:
                return lines_q.popleft()
            return None

    try:
        while True:
            for key in reader.drain():
                if key.name in ("ctrl-c", "esc", "ctrl-d"):
                    raise GenerationInterrupted()
                reader.unget(key)  # 打字先行：留给下一轮输入
            item = _next_item()
            now = time.time()
            if item is None:
                if now - last_partial > 0.15 and md.has_partial():
                    screen.write_stream(md.partial())
                    last_partial = now
                elif busy_text or active_tools:
                    if active_tools:
                        label = " · ".join(
                            f"{name} {fmt_dur(now - start)}"
                            for _tid, (name, start) in active_tools.items()
                        )
                    else:
                        label = busy_text or ""
                    screen.busy_line(paint(SPINNER[frame % 4] + " " + label, "yellow"))
                    frame += 1
                continue
            if isinstance(item, tuple):
                if item[0] == "__error__":
                    screen.newline()
                    screen.write(paint(f"✖ 流式连接出错：{item[1]}", "red"))
                break
            line = item.rstrip("\n")
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except Exception:
                continue
            name, data = event.get("event"), event.get("data") or {}
            if name == "session":
                run_id = data.get("run_id")
                conv_id = data.get("conversation_id") or conv_id
                if conv_id:
                    save_session(os.getcwd(), int(conv_id))
            elif name == "status":
                busy_text = safe_text(data.get("text") or "")
            elif name == "plan":
                screen.write(paint("📋 执行计划", "cyan", bold=True))
                for i, step in enumerate(data.get("steps") or [], 1):
                    screen.write(paint(f"   {i}. {safe_text(step)}", "cyan"))
            elif name == "plan_progress":
                done = int(data.get("done") or 0)
                total = int(data.get("total") or 0)
                if total > 0:
                    width = max(10, term_width() - 24)
                    filled = int(width * done / total)
                    bar = "█" * filled + "░" * (width - filled)
                    screen.write(paint(f"   {bar} {done}/{total}", "dim"))
            elif name == "todos":
                screen.write(paint("任务清单", "cyan", bold=True))
                for t in data.get("todos") or []:
                    mark = "[x]" if t.get("done") else "[ ]"
                    color = "green" if t.get("done") else "dim"
                    screen.write(paint(f"   {mark} {safe_text(t.get('text', ''))}", color))
            elif name == "reasoning":
                screen.write(paint("💭 已深度思考（摘要）", "cyan", bold=True))
                screen.write(paint(f"   {clip(safe_text(data.get('summary', '')), term_width() - 3)}", "cyan"))
            elif name == "tool_start":
                tid = data.get("id") or data.get("name")
                active_tools[str(tid)] = (safe_text(data.get("name", "tool")), time.time())
                screen.write(
                    paint("⏺ ", "cyan")
                    + paint(safe_text(data.get("name", "tool")), "cyan", bold=True)
                    + paint(f"({render_tool_args(data.get('arguments'))})", "dim")
                )
            elif name == "tool_result":
                tid = str(data.get("id") or "")
                started = active_tools.pop(tid, None)
                dur_ms = data.get("duration_ms")
                if started is None:
                    dur = fmt_dur(float(dur_ms) / 1000) if isinstance(dur_ms, (int, float)) else ""
                else:
                    dur = fmt_dur(time.time() - started[1])
                summary = clip(safe_text(data.get("summary") or ""), term_width() - 8)
                suffix = f"  ({dur})" if dur else ""
                color = "red" if data.get("error") else "green"
                # 完整输出留档：Ctrl+O / /output n 可展开查看
                record_tool_output(
                    safe_text(data.get("name", "tool")),
                    safe_text(data.get("summary") or ""),
                    safe_text(data.get("detail") or ""),
                )
                screen.write(paint(f"⎿ {summary}{suffix}", color))
                if not active_tools:
                    busy_text = None
            elif name == "permission_request":
                prompt_permission(reader, data, base_url)
            elif name == "permission_resolved":
                if data.get("approved"):
                    screen.write(paint("   ▶ 已批准", "green"))
                else:
                    screen.write(paint(f"   ▶ 已拒绝：{safe_text(data.get('reason', ''))}", "red"))
            elif name == "token":
                token = safe_text(data if isinstance(data, str) else str(data))
                busy_text = None
                screen.write_stream(md.feed(token))
                last_partial = time.time()
            elif name == "title":
                screen.write(paint(f"[标题：{safe_text(data.get('title', ''))}]", "dim"))
            elif name == "hook":
                screen.write(
                    paint(
                        f"🪝 {safe_text(data.get('event', 'hook'))} {safe_text(data.get('name', ''))}",
                        "dim",
                    )
                )
            elif name == "done":
                rest = md.flush()
                screen.write_stream(rest)
                screen.newline()
                usage = fetch_usage(base_url, conv_id)
                # 回答完成后刷新上下文占用（输入区状态行的 meter 数据源）
                stats = fetch_context_stats(base_url, conv_id)
                elapsed = fmt_dur(time.time() - t0)
                bits = [f"会话 {conv_id}", tool_mode, f"耗时 {elapsed}"]
                tok = fmt_tokens(usage)
                if tok:
                    bits.append(tok)
                if stats:
                    bits.append(f"上下文 {context_pct(stats)}%")
                screen.write(paint("─ " + " · ".join(bits) + " ─", "dim"))
                # 长任务完成提示音（>10s）：切走窗口也能知道回答完了
                if time.time() - t0 > 10:
                    try:
                        sys.stdout.write("\a")
                        sys.stdout.flush()
                    except Exception:
                        pass
                break
            elif name == "error":
                screen.newline()
                message = safe_text(data.get("message", "未知错误"))
                hint = (
                    "（后端与模型供应商的连接失败，请检查后端日志/网络后重试）"
                    if "connection" in message.lower() or "10013" in message
                    else ""
                )
                screen.write(paint(f"✖ {message}{hint}", "red"))
    except (GenerationInterrupted, KeyboardInterrupt):
        interrupted = True
    finally:
        done_flag.set()
        if interrupted:
            try:
                cancelled = _cancel_run(base_url, run_id)
            except KeyboardInterrupt:
                # 打断清理期间再按 Ctrl+C：不再逃逸，视为已打断即可
                cancelled = False
            # 立即关闭连接，让后端 SSE 生成器尽快感知断开
            try:
                for resp in resp_holder:
                    resp.close()
            except Exception:
                pass
            screen.newline()
            screen.write(paint("⏹ 已打断生成" + ("（后端已停止）" if cancelled else "（已断开连接）"), "yellow"))
            screen.write(paint("   按 ↑ 可找回刚才的问题，回车继续对话。", "dim"))
    return conv_id  # type: ignore[return-value]


# ------------------------------------------------------------------ 主流程


def run_headless(
    base_url: str,
    question: str,
    tool_mode: str,
    conversation_id: int | None,
    sandbox: str | None,
    output_format: str,
) -> int:
    """headless 一次性问答（类 claude -p）：无交互 UI，收集事件后输出退出。

    text 输出回答正文；json 输出 {answer, conversation_id, sources,
    tool_trace, token_usage, timings}；出错时错误进 stderr 并返回 1。
    """
    body = json.dumps(
        {
            "question": question,
            "conversation_id": conversation_id,
            "tool_mode": tool_mode,
            "template_id": None,
            "project_dir": os.getcwd(),
            "command_sandbox": sandbox,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/agent/stream",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    answer: list[str] = []
    sources: list[dict] = []
    tool_trace: list[dict] = []
    conv_id: int | None = conversation_id
    error: str | None = None
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except Exception:
                    continue
                name, data = event.get("event"), event.get("data") or {}
                if name == "session":
                    conv_id = data.get("conversation_id") or conv_id
                elif name == "token":
                    answer.append(data if isinstance(data, str) else str(data))
                elif name == "tool_result":
                    tool_trace.append(
                        {"name": data.get("name"), "summary": data.get("summary")}
                    )
                    if data.get("sources"):
                        sources.extend(data["sources"])
                elif name == "error":
                    error = str(data.get("message") or "未知错误")
    except Exception as exc:
        error = str(exc)
    if error:
        msg = (
            json.dumps({"error": error}, ensure_ascii=False)
            if output_format == "json"
            else f"✖ {error}"
        )
        print(msg, file=sys.stderr)
        return 1
    text = "".join(answer)
    if output_format == "json":
        usage, timings = {}, {}
        try:
            runs = http_json(
                base_url, "GET", f"/api/runs?conversation_id={conv_id}&limit=1", timeout=10
            )
            if isinstance(runs, list) and runs:
                usage = (runs[0].get("token_usage") or {}) if isinstance(runs[0], dict) else {}
                timings = usage.get("timings") or {}
        except Exception:
            pass
        print(
            json.dumps(
                {
                    "conversation_id": conv_id,
                    "answer": text,
                    "sources": sources,
                    "tool_trace": tool_trace,
                    "token_usage": usage,
                    "timings": timings,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(text)
    return 0


def print_banner(model: str | None, tool_mode: str, has_agents: bool) -> None:
    """盒式欢迎横幅 + 命令/快捷键总览（类 Claude Code 启动页）。"""
    _boxed(
        [
            f"模型 {model or '未知'} · 工具模式 {tool_mode}"
            + (" · 已加载 AGENTS.md" if has_agents else "")
        ],
        "个人知识库 RAG 智能助手 · 终端版",
        "cyan",
    )
    print(
        paint(
            "命令：/context 上下文 · /compact 压缩 · /rewind 回退 · /output [n] 展开输出\n"
            "      /todos 任务 · /cost 用量 · /status 状态 · /tools 切换 · /new 新会话 · /resume 恢复\n"
            "      /init 创建 AGENTS.md · /memory 项目记忆 · /clear 清屏 · /exit 退出\n"
            "快捷：? 帮助面板 · Ctrl+O 展开输出 · Ctrl+R 重发 · Ctrl+L 清屏 · ↑/↓ 历史 · Ctrl+C 打断",
            "dim",
        )
    )
    if not has_agents:
        print(paint("提示：/init 创建 AGENTS.md（项目记忆）", "dim"))


def main() -> None:
    if sys.platform == "win32":
        try:
            os.system("chcp 65001 >nul")
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="终端版 Agent 客户端（类 Claude Code）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--tool", default="auto", choices=["auto", "knowledge", "web", "none"]
    )
    parser.add_argument(
        "--sandbox",
        default=None,
        choices=["subprocess", "docker"],
        help="命令沙箱：docker=容器沙箱（无网络/只读根/限资源），subprocess=宿主执行；不传跟随设置页",
    )
    parser.add_argument("--conversation", type=int, default=None)
    parser.add_argument(
        "-p",
        "--print",
        dest="print_query",
        default=None,
        help="headless 模式：执行一次问答后退出（类 claude -p），不进入交互界面",
    )
    parser.add_argument(
        "--output-format",
        default="text",
        choices=["text", "json"],
        help="headless 输出格式：text=回答正文，json=完整结构（answer/sources/usage/timings）",
    )
    args = parser.parse_args()

    base = f"http://{args.host}:{args.port}"
    tool_mode = args.tool
    sandbox_mode: str | None = args.sandbox
    conversation_id = args.conversation
    cwd = os.getcwd()
    history = load_history(cwd)
    last_question = ""
    model = fetch_model_label(base)

    # headless：一次性问答后退出（可接脚本/CI/管道组合）
    if args.print_query:
        sys.exit(
            run_headless(
                base,
                args.print_query,
                tool_mode,
                conversation_id,
                sandbox_mode,
                args.output_format,
            )
        )

    reader = KeyReader()
    screen = Screen()
    last_ctrl_c = 0.0  # 空提示符下双击 Ctrl+C 退出（Claude Code 风格）
    pending_prefill = ""  # /rewind 后放回输入框的预填文本
    try:
        has_agents = os.path.exists(os.path.join(cwd, "AGENTS.md"))
        print_banner(model, tool_mode, has_agents)
        while True:
            try:
                # 输入区状态行：模型/工具/会话/沙箱/上下文 meter（对话历史与输入框之间有分隔线）
                question = read_line(
                    reader,
                    history,
                    default=pending_prefill,
                    status=build_status_line(model, tool_mode, conversation_id, sandbox_mode),
                )
                pending_prefill = ""
            except Interrupted as exc:
                print()
                pending_prefill = ""
                if exc.had_text:
                    # 输入框有内容：Ctrl+C/Esc 清空输入，留在 CLI
                    continue
                # 空提示符：第一次 Ctrl+C 提示，1.5s 内再按一次退出回终端
                now = time.time()
                if now - last_ctrl_c <= 1.5:
                    break
                last_ctrl_c = now
                print(paint("再按一次 Ctrl+C 退出 CLI", "dim"))
                continue
            except QuitRequested:
                print()
                break
            except OutputRequested:
                # Ctrl+O：展开最近一次工具调用的完整输出
                show_tool_output(1)
                continue
            except RetryRequested:
                if not last_question:
                    print(paint("还没有上一条问题。", "yellow"))
                    continue
                print(paint(f"› {last_question}", "green"))
                question = last_question

            question = question.strip()
            if not question:
                continue
            save_history(cwd, history)
            if question.startswith("/"):
                cmd, _, arg = question.partition(" ")
                arg = arg.strip()
                if cmd in ("/exit", "/quit"):
                    break
                if cmd == "/clear":
                    _clear_screen()
                    print_banner(model, tool_mode, has_agents)
                    continue
                if cmd == "/new":
                    conversation_id = None
                    LAST_CONTEXT["stats"] = None
                    try:
                        os.remove(os.path.join(cwd, SESSION_FILE))
                    except OSError:
                        pass
                    print(paint("已开启新会话。", "dim"))
                    continue
                if cmd == "/resume":
                    session_history = load_session_history(cwd)
                    if not session_history:
                        print(paint("本目录还没有可恢复的会话。", "yellow"))
                        continue
                    index = 0
                    if arg.isdigit() and 1 <= int(arg) <= len(session_history):
                        index = int(arg) - 1
                    elif len(session_history) > 1:
                        titles = fetch_conversation_titles(base)
                        print(paint("最近会话（输入序号恢复）：", "cyan", bold=True))
                        for i, h in enumerate(session_history, 1):
                            cid = h.get("conversation_id")
                            title = titles.get(int(cid), "")
                            # 有标题优先显示标题；无标题（新会话未生成）显示会话号
                            label = title if title and title != "新对话" else f"会话 {cid}"
                            print(
                                paint(
                                    f"  {i}. {label}"
                                    + (f" · #{cid}" if title and title != "新对话" else ""),
                                    "dim",
                                )
                            )
                        try:
                            sel = read_line(reader, []).strip()
                        except Interrupted:
                            sel = ""
                        index = (
                            int(sel) - 1
                            if sel.isdigit() and 1 <= int(sel) <= len(session_history)
                            else 0
                        )
                    conversation_id = int(session_history[index].get("conversation_id"))
                    titles = fetch_conversation_titles(base)
                    title = titles.get(conversation_id, "")
                    label = title if title and title != "新对话" else f"会话 {conversation_id}"
                    print(paint(f"已恢复会话 {conversation_id}（{label}）。", "dim"))
                    fetch_context_stats(base, conversation_id)  # 状态行 meter 换源
                    # 加载并显示最近的对话上下文，方便确认从哪继续
                    recent = fetch_conversation_messages(base, conversation_id)
                    if recent:
                        print(paint("── 最近对话 ──", "cyan", bold=True))
                        for m in recent:
                            role = m.get("role")
                            content = safe_text(m.get("content") or "")[:300]
                            if role == "user":
                                print(paint(f"  你：{content}", "green"))
                            elif role == "assistant":
                                # 工具轮消息可能含大量 XML/JSON，只显示摘要前段
                                snippet = content[:200]
                                if "<tool_calls" in content or "<invoke" in content:
                                    snippet = "（工具调用步骤）"
                                print(paint(f"  AI：{snippet}", "dim"))
                    continue
                if cmd == "/init":
                    init_project_memory(cwd)
                    has_agents = True
                    continue
                if cmd == "/memory":
                    show_project_memory(cwd)
                    continue
                if cmd == "/tools":
                    if arg in ("auto", "knowledge", "web", "none"):
                        tool_mode = arg
                        print(paint(f"工具模式已切换为：{tool_mode}", "dim"))
                    else:
                        print(paint("用法：/tools auto|knowledge|web|none", "yellow"))
                    continue
                if cmd == "/todos":
                    fetch_todos(base, conversation_id)
                    continue
                if cmd == "/cost":
                    show_cost(base, conversation_id)
                    continue
                if cmd == "/context":
                    show_context(base, conversation_id)
                    continue
                if cmd == "/compact":
                    run_compact(base, conversation_id)
                    continue
                if cmd == "/rewind":
                    prefill = run_rewind(base, conversation_id, reader, arg)
                    if prefill:
                        pending_prefill = prefill
                    fetch_context_stats(base, conversation_id)  # 消息变少，meter 同步
                    continue
                if cmd == "/output":
                    n = int(arg) if arg.isdigit() else 1
                    show_tool_output(n)
                    continue
                if cmd == "/sandbox":
                    if arg in ("docker", "subprocess"):
                        sandbox_mode = arg
                        tip = "容器沙箱（无网络/只读根/限资源）" if arg == "docker" else "宿主执行"
                        print(paint(f"命令沙箱已切换为：{arg}（{tip}）。", "dim"))
                    elif arg in ("off", "auto", ""):
                        sandbox_mode = None
                        print(paint("命令沙箱：跟随设置页配置（/sandbox docker|subprocess 切换）。", "dim"))
                    else:
                        print(paint("用法：/sandbox docker|subprocess|off", "yellow"))
                    continue
                if cmd == "/status":
                    bits = [
                        f"模式={tool_mode}",
                        f"会话={conversation_id or '新会话'}",
                        f"模型={model or '未知'}",
                        f"沙箱={sandbox_mode or '跟随设置'}",
                    ]
                    meter = ctx_meter(LAST_CONTEXT.get("stats"))
                    if meter:
                        bits.append(meter)
                    print(paint(" · ".join(bits), "dim"))
                    continue
                if cmd == "/help":
                    print(
                        paint(
                            "/exit /quit 退出 · /new 新会话 · /tools auto|knowledge|web|none\n"
                            "/todos 任务清单 · /status 状态 · /cost 用量 · /clear 清屏\n"
                            "/context 上下文占用 · /compact 手动压缩 · /rewind 消息回退\n"
                            "/output [n] 展开工具输出 · /sandbox docker|subprocess|off 命令沙箱\n"
                            "/init 创建 AGENTS.md · /memory 项目记忆 · /resume 恢复会话\n"
                            "Ctrl+C/Esc 打断 · Ctrl+L 清屏 · Ctrl+R 重发 · Ctrl+O 展开输出 · ↑/↓ 历史",
                            "dim",
                        )
                    )
                    continue
                print(paint(f"未知命令：{cmd}（/help 查看）", "yellow"))
                continue
            last_question = question
            conversation_id = stream_question(
                base, question, tool_mode, conversation_id, reader, screen, sandbox_mode
            )
    except KeyboardInterrupt:
        print()
    finally:
        reader.stop()
        save_history(cwd, history)
        print(paint("再见。", "dim"))


if __name__ == "__main__":
    sys.exit(main())
