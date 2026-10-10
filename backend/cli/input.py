"""按键读取与行编辑（Windows/Unix 两套实现）+ 命令补全。

Interrupted/QuitRequested/RetryRequested/OutputRequested 也在这里：由 read_line 抛出、主循环捕获。
从 cli_agent.py 拆出（2026-10-08 物理拆分）：只做移动，未改逻辑。"""

from __future__ import annotations

import collections
import os
import sys
import threading
import time
from cli.term import USE_COLOR, clip, paint, strwidth, term_width


# "?" 键展开的命令帮助面板开关（本层行编辑 UI 的状态机）
HELP_OPEN: dict = {"on": False}


COMMANDS = [
    "/exit",
    "/quit",
    "/new",
    "/continue",
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


_INPUT_ROWS = [1]  # 上次输入块占用的终端行数（含菜单行），用于回清残留


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
