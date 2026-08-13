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
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

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
                if not msvcrt.kbhit():
                    time.sleep(0.015)
                    continue
                ch = msvcrt.getwch()
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


COMMANDS = [
    "/exit",
    "/quit",
    "/new",
    "/tools",
    "/todos",
    "/status",
    "/cost",
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


def complete(buf: str) -> tuple[list[str], str]:
    """返回 (候选列表, 幽灵补全文本)。"""
    if " " in buf and buf.split(" ", 1)[0] == "/tools":
        _, _, arg = buf.partition(" ")
        values = ["auto", "knowledge", "web", "none"]
        matches = [v for v in values if v.startswith(arg) and v != arg]
        cp = _common_prefix(matches)
        return matches, cp[len(arg) :] if cp else ""
    if buf.startswith("/"):
        word = buf.split(" ", 1)[0]
        matches = [c for c in COMMANDS if c.startswith(word)]
        cp = _common_prefix(matches)
        ghost = cp[len(word) :] if len(cp) > len(word) else ""
        return matches, ghost
    return [], ""


def _render_input(buf: str, pos: int) -> None:
    matches, ghost = complete(buf)
    sys.stdout.write("\r\033[2K")
    sys.stdout.write(paint("› ", "green", bold=True) + buf + paint(ghost, "dim"))
    col = strwidth("› " + buf[:pos])
    sys.stdout.write(f"\033[{col + 1}G")
    if buf.startswith("/") and matches:
        sys.stdout.write(
            "\033[1B\r\033[2K  " + "  ".join(clip(m, 26) for m in matches[:6])
        )
        sys.stdout.write(f"\033[1A\033[{col + 1}G")
    sys.stdout.flush()


def read_line(reader: KeyReader, history: list[str]) -> str:
    """带历史/光标/补全的行编辑；Ctrl+C/Esc/D/R 抛对应异常。"""
    buf = ""
    pos = 0
    draft = ""
    idx = len(history)
    sys.stdout.write(paint("› ", "green", bold=True))
    sys.stdout.flush()
    while True:
        key = reader.get()
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
            elif key.name == "ctrl-r":
                raise RetryRequested()
            elif key.name == "enter":
                sys.stdout.write("\n")
                sys.stdout.flush()
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


def prompt_permission(reader: KeyReader, data: dict, base_url: str) -> None:
    """渲染审批弹窗并等待数字键决定（回车=批准，Esc=拒绝）。"""
    print()
    args = data.get("arguments") or {}
    rows = [clip(f"{k}: {safe_text(json.dumps(v, ensure_ascii=False))}", term_width() - 6) for k, v in (args.items() if isinstance(args, dict) else [])]
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
        print()
        approve = answer in ("1", "3", "4")
        remember_session = answer == "3" and is_command
        remember_forever = answer == "4" and is_command
        if approve:
            try:
                reason = read_line(reader, [], ).strip()
            except Interrupted:
                reason = ""
            ok = resolve_permission(
                base_url,
                data.get("id", ""),
                True,
                reason,
                remember_forever=remember_forever,
                remember_session=remember_session,
            )
            print(paint("✔ 已批准，继续执行…", "green") if ok else paint("✘ 提交失败，请检查后端", "red"))
            return
        try:
            reason = read_line(reader, [], ).strip()
        except Interrupted:
            reason = ""
        ok = resolve_permission(base_url, data.get("id", ""), False, reason)
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
) -> int:
    """发送问题并渲染 SSE 事件；Ctrl+C/Esc 即时打断并通知后端取消。"""
    body = json.dumps(
        {
            "question": question,
            "conversation_id": conversation_id,
            "tool_mode": tool_mode,
            "template_id": None,
            "project_dir": os.getcwd(),
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
                    paint(
                        f"⏺ {safe_text(data.get('name', 'tool'))}({render_tool_args(data.get('arguments'))})",
                        "dim",
                    )
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
                elapsed = fmt_dur(time.time() - t0)
                bits = [f"会话 {conv_id}", tool_mode, f"耗时 {elapsed}"]
                tok = fmt_tokens(usage)
                if tok:
                    bits.append(tok)
                screen.write(paint("─ " + " · ".join(bits) + " ─", "dim"))
                break
            elif name == "error":
                screen.newline()
                screen.write(paint(f"✖ {safe_text(data.get('message', '未知错误'))}", "red"))
    except (GenerationInterrupted, KeyboardInterrupt):
        interrupted = True
    finally:
        done_flag.set()
        if interrupted:
            cancelled = _cancel_run(base_url, run_id)
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


def print_banner(model: str | None, tool_mode: str, has_agents: bool) -> None:
    print(paint("个人知识库 RAG 智能助手 · 终端版", "cyan", bold=True))
    print(paint(f"模型：{model or '未知'} · 工具模式：{tool_mode}", "dim"))
    print(
        paint(
            "↑/↓ 历史 · Ctrl+C 打断 · Ctrl+L 清屏 · Ctrl+R 重发 · /help 命令",
            "dim",
        )
    )
    if has_agents:
        print(paint("已加载本目录 AGENTS.md（/memory 查看加载链）", "dim"))
    else:
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
    parser.add_argument("--conversation", type=int, default=None)
    args = parser.parse_args()

    base = f"http://{args.host}:{args.port}"
    tool_mode = args.tool
    conversation_id = args.conversation
    cwd = os.getcwd()
    history = load_history(cwd)
    last_question = ""
    model = fetch_model_label(base)

    reader = KeyReader()
    screen = Screen()
    try:
        has_agents = os.path.exists(os.path.join(cwd, "AGENTS.md"))
        print_banner(model, tool_mode, has_agents)
        while True:
            try:
                question = read_line(reader, history)
            except Interrupted as exc:
                print()
                if exc.had_text:
                    continue
                break
            except QuitRequested:
                print()
                break
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
                            print(
                                paint(
                                    f"  {i}. 会话 {cid} · {titles.get(int(cid), '')}",
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
                    print(paint(f"已恢复会话 {conversation_id}。", "dim"))
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
                if cmd == "/status":
                    print(
                        paint(
                            f"模式={tool_mode} · 会话={conversation_id or '新会话'} · 模型={model or '未知'}",
                            "dim",
                        )
                    )
                    continue
                if cmd == "/help":
                    print(
                        paint(
                            "/exit /quit 退出 · /new 新会话 · /tools auto|knowledge|web|none\n"
                            "/todos 任务清单 · /status 状态 · /cost 用量 · /clear 清屏\n"
                            "/init 创建 AGENTS.md · /memory 项目记忆 · /resume 恢复会话\n"
                            "Ctrl+C/Esc 打断生成 · Ctrl+L 清屏 · Ctrl+R 重发 · ↑/↓ 历史",
                            "dim",
                        )
                    )
                    continue
                print(paint(f"未知命令：{cmd}（/help 查看）", "yellow"))
                continue
            last_question = question
            conversation_id = stream_question(
                base, question, tool_mode, conversation_id, reader, screen
            )
    except KeyboardInterrupt:
        print()
    finally:
        reader.stop()
        save_history(cwd, history)
        print(paint("再见。", "dim"))


if __name__ == "__main__":
    sys.exit(main())
