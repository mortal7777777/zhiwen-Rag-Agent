"""按键读取与行编辑（Windows/Unix 两套实现）+ 命令补全。

Interrupted/QuitRequested/RetryRequested/OutputRequested 也在这里：由 read_line 抛出、主循环捕获。
从 cli_agent.py 拆出（2026-10-08 物理拆分）：只做移动，未改逻辑。"""

from __future__ import annotations

import collections
import os
import sys
import threading
import time
from cli.term import (
    ASCII_MODE,
    THEME,
    USE_COLOR,
    clip,
    paint,
    strwidth,
    term_height,
    term_width,
)


# "?" 键展开的命令帮助面板开关（本层行编辑 UI 的状态机）
HELP_OPEN: dict = {"on": False}


COMMANDS = [
    "/help",
    "/new",
    "/resume",
    "/timeline",
    "/model",
    "/export",
    "/context",
    "/cost",
    "/status",
    "/todos",
    "/compact",
    "/rewind",
    "/output",
    "/tools",
    "/sandbox",
    "/doctor",
    "/mcp",
    "/skills",
    "/init",
    "/memory",
    "/continue",
    "/abandon",
    "/clear",
    "/exit",
    "/quit",
]

# / 命令面板的描述（类 Claude Code 的命令菜单）
COMMAND_META = {
    "/exit": "退出 CLI",
    "/quit": "退出 CLI",
    "/new": "开启新会话",
    "/continue": "续跑被中断的一轮",
    "/abandon": "丢弃被中断的一轮",
    "/tools": "切换工具模式",
    "/model": "切换对话供应商",
    "/export": "导出当前会话",
    "/todos": "查看任务清单",
    "/status": "查看运行状态",
    "/cost": "查看用量、缓存与成本",
    "/context": "查看上下文占用",
    "/compact": "手动压缩上下文",
    "/rewind": "回退到历史消息",
    "/output": "分页查看工具输出",
    "/sandbox": "切换命令沙箱",
    "/clear": "清屏并重绘横幅",
    "/init": "创建 AGENTS.md",
    "/resume": "恢复历史会话",
    "/timeline": "检查点时间线 / 回滚",
    "/doctor": "环境诊断（后端/索引/MCP/技能）",
    "/mcp": "查看 MCP 服务器",
    "/skills": "查看技能列表",
    "/memory": "查看项目记忆",
    "/help": "查看完整帮助",
}


MENU = {"prefix": "", "index": 0}


def _command_matches(text: str) -> list[str]:
    """纯命令前缀（不含参数）匹配；出现任何空白说明用户已在输入参数。"""
    word = (text or "").lstrip()
    if not word.startswith("/") or any(ch.isspace() for ch in word):
        return []
    return [c for c in COMMANDS if c.startswith(word)]


def menu_matches(text: str) -> list[str]:
    """返回命令菜单候选；前缀变化时把选中项重置到第一条。"""
    matches = _command_matches(text)
    prefix = (text or "").strip()
    if MENU["prefix"] != prefix:
        MENU["prefix"] = prefix
        MENU["index"] = 0
    if matches:
        MENU["index"] = max(0, min(MENU["index"], len(matches) - 1))
    return matches


def menu_move(text: str, step: int) -> list[str]:
    matches = menu_matches(text)
    if matches:
        MENU["index"] = (MENU["index"] + step) % len(matches)
    return matches


def menu_selected(text: str) -> str | None:
    matches = menu_matches(text)
    return matches[MENU["index"]] if matches else None


_INPUT_ROWS = [1]  # 上次输入块占用的终端行数（含菜单行），用于回清残留
_CURSOR_ROW = [0]  # 光标在输入块内的行号（0=输入首行；菜单在输入块下方）
_FALLBACK_NOTICE = [False]


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
                        "I": "pageup",
                        "Q": "pagedown",
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
        elif ch == "\x19":
            name = "ctrl-y"
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
            b"[1~": "home",
            b"[4~": "end",
            b"[7~": "home",
            b"[8~": "end",
            b"[3~": "delete",
            b"[5~": "pageup",
            b"[6~": "pagedown",
        }
        if data.startswith(b"\x1b[") and data[2:] in mapping:
            self._push(Key(mapping[data[2:]]))
            return
        for byte in data:
            self._push_key(chr(byte))


_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".idea", ".vscode",
}


def _at_fragment(text: str) -> tuple[str, int] | None:
    """光标前最后一个空白分隔 token 是 @路径 时，返回 (片段, 替换长度)。"""
    i = len(text) - 1
    while i >= 0 and not text[i].isspace():
        i -= 1
    token = text[i + 1 :]
    if not token.startswith("@"):
        return None
    fragment = token[1:]
    if fragment[:1] in ("'", '"'):
        return None  # 引号路径不做补全，用户自行补齐
    return fragment, -len(fragment)


def file_completions(fragment: str) -> list[str]:
    """@路径 的文件名补全（目录补 /，含空格的路径自动加引号）。"""
    expanded = os.path.expanduser(fragment)
    if os.path.isabs(expanded) or (len(expanded) >= 2 and expanded[1] == ":"):
        base, prefix = os.path.dirname(expanded), os.path.basename(expanded)
    else:
        base, prefix = os.path.dirname(fragment), os.path.basename(fragment)
    scan_dir = os.path.expanduser(base or ".")
    try:
        entries = list(os.scandir(scan_dir))
    except OSError:
        return []

    def key(entry) -> tuple:
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False
        return (not is_dir, entry.name.lower())

    out: list[str] = []
    for entry in sorted(entries, key=key):
        name = entry.name
        if not name.startswith(prefix):
            continue
        if name.startswith(".") and not prefix.startswith("."):
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False
        if is_dir and name in _SKIP_DIRS and not prefix:
            continue
        rel = (
            base + name
            if not base or base.endswith(("/", "\\"))
            else base + "/" + name
        )
        if is_dir:
            rel += "/"
        if " " in rel:
            rel = f'"{rel}"'
        out.append(rel)
        if len(out) >= 50:
            break
    return out


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
    old_total = max(1, _INPUT_ROWS[0])
    old_cursor_row = _CURSOR_ROW[0]

    sys.stdout.write("\033[?25l")  # 隐藏光标，减少闪烁
    # 1) 从旧光标行回到输入块顶部
    if old_cursor_row:
        sys.stdout.write(f"\033[{old_cursor_row}A")
    sys.stdout.write("\r")
    # 2) 清掉旧块（输入行 + 菜单行）：光标当前在块顶，向下逐行清
    for i in range(old_total):
        sys.stdout.write("\033[2K")
        if i < old_total - 1:
            sys.stdout.write("\033[1B\r")
    if old_total > 1:
        sys.stdout.write(f"\033[{old_total - 1}A")
    sys.stdout.write("\r")
    # 3) 写出新内容（终端按 cols 自动换行），光标停在最后一行
    sys.stdout.write(line)
    # 4) 从行尾回到块顶，再定位到 pos（菜单在输入块下方，光标基准是 pos_row）
    if rows > 1:
        sys.stdout.write(f"\033[{rows - 1}A")
    sys.stdout.write("\r")
    if pos_row:
        sys.stdout.write(f"\033[{pos_row}B")
    if pos_col:
        sys.stdout.write(f"\033[{pos_col + 1}G")
    # 5) / 命令菜单（画在输入块下方一行，再回到光标处）
    menu_rows = 0
    command_list = menu_matches(buf) if with_ghost and buf.startswith("/") else []
    menu_text = ""
    if command_list:
        selected = command_list[MENU["index"]]
        mark = "> " if ASCII_MODE else "❯ "
        menu_text = (
            f"{mark}{selected}  {COMMAND_META.get(selected, '')}"
            f"   ↑/↓ 选择 · 回车执行 · {MENU['index'] + 1}/{len(command_list)}"
        )
    elif with_ghost and matches and " " in buf:
        menu_text = "  " + "  ".join(clip(m, 26) for m in matches[:6])
    if menu_text:
        down = rows - pos_row
        if down:
            sys.stdout.write(f"\033[{down}B")
        sys.stdout.write("\r\033[2K")
        sys.stdout.write(clip(menu_text, cols))
        menu_rows = 1
        if down:
            sys.stdout.write(f"\033[{down}A")
        sys.stdout.write("\r")
        if pos_col:
            sys.stdout.write(f"\033[{pos_col + 1}G")
    _INPUT_ROWS[0] = rows + menu_rows
    _CURSOR_ROW[0] = pos_row
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()


def _toolbar_text():
    """底部工具栏：/ 命令菜单（可上下选择）或常驻快捷键提示。"""
    from prompt_toolkit.application import get_app

    key = lambda s: ("class:tbkey", s)
    txt = lambda s: ("class:tb", s)
    try:
        buf_text = get_app().current_buffer.text
    except Exception:
        buf_text = ""

    menu_list = menu_matches(buf_text) if buf_text.strip().startswith("/") else []
    if menu_list:
        rows = max(3, min(6, term_height() - 8))
        idx = MENU["index"]
        start = max(0, min(idx - rows // 2, len(menu_list) - rows))
        mark = "> " if ASCII_MODE else "❯ "
        out: list[tuple[str, str]] = []
        for i in range(start, min(start + rows, len(menu_list))):
            cmd = menu_list[i]
            style = "class:menu.current" if i == idx else "class:menu"
            prefix = mark if i == idx else "  "
            out.append((style, f"{prefix}{cmd:<14} {COMMAND_META.get(cmd, '')}\n"))
        out.append(
            (
                "class:menu.hint",
                f"↑/↓ 选择 · 回车执行 · {idx + 1}/{len(menu_list)}",
            )
        )
        return out

    if not HELP_OPEN["on"]:
        return [
            key(" ? "),
            txt("命令帮助   "),
            key(" / "),
            txt("命令补全   "),
            key("@"),
            txt("文件补全   "),
            key("Ctrl+J "),
            txt("换行   "),
            key("Ctrl+O "),
            txt("分页查看   "),
            key("Ctrl+R "),
            txt("历史搜索   "),
            key("Ctrl+Y "),
            txt("重发   "),
            key("Ctrl+C "),
            txt("打断 / 双击退出"),
        ]
    return [
        key(" /context "), txt("上下文占用   "),
        key(" /compact "), txt("手动压缩   "),
        key(" /rewind "), txt("消息回退   "),
        key(" /output [n] "), txt("分页查看工具输出\n"),
        key(" /todos "), txt("任务清单   "),
        key(" /cost "), txt("用量   "),
        key(" /status "), txt("状态   "),
        key(" /model "), txt("切换模型   "),
        key(" /export "), txt("导出会话\n"),
        key(" /timeline "), txt("快照回滚   "),
        key(" /doctor "), txt("环境诊断   "),
        key(" /mcp "), txt("MCP   "),
        key(" /skills "), txt("技能\n"),
        key(" /tools auto|knowledge|web|none\n"),
        key(" /new "), txt("新会话   "),
        key(" /resume "), txt("恢复会话   "),
        key(" /init "), txt("创建 AGENTS.md   "),
        key(" /memory "), txt("项目记忆\n"),
        key(" /clear "), txt("清屏   "),
        key(" /exit "), txt("退出   "),
        key(" ? "), txt("收起面板"),
    ]


def completion_candidates(text: str) -> list[tuple[str, int, str]]:
    """返回 (补全文本, 替换起点, 描述)；命令菜单与测试共用。

    替换起点用空白分隔的最后一个 token（而不是 prompt_toolkit 的
    get_word_before_cursor——它把 "/" 当标点，会把 /mo 补成 //model）。
    """
    at = _at_fragment(text)
    if at is not None:
        fragment, start = at
        return [(path, start, "") for path in file_completions(fragment)]
    matches, _ = complete(text)
    i = len(text)
    while i > 0 and not text[i - 1].isspace():
        i -= 1
    start = -(len(text) - i)
    return [(m, start, COMMAND_META.get(m, "")) for m in matches]


def make_prompt_session(history: list[str], submit_empty: bool = True):
    """构建 prompt_toolkit PromptSession：历史 + Tab 补全 + 键绑定语义。

    取代手写 KeyReader 行编辑（Windows 终端上末字符不可见/光标错位/
    Ctrl+C 竞争等 bug），与 Hermes CLI 同款方案。
    底部工具栏常驻快捷键提示，空输入按 ? 展开/收起完整命令面板。
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import get_app
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings

    class _CmdCompleter(Completer):
        def get_completions(self, document, complete_event):
            # 纯 / 命令交给底栏菜单（保证在 cmd/PowerShell 也可见）；
            # 参数补全与 @ 文件补全仍走 completion 弹层。
            if _command_matches(document.text_before_cursor):
                return
            for value, start, meta in completion_candidates(
                document.text_before_cursor
            ):
                yield Completion(
                    value, start_position=start, display=value, display_meta=meta
                )

    kb = KeyBindings()

    def menu_active() -> bool:
        try:
            return bool(_command_matches(get_app().current_buffer.text))
        except Exception:
            return False

    @kb.add("up", filter=Condition(menu_active))
    def _(event):
        menu_move(event.current_buffer.text, -1)
        event.app.invalidate()

    @kb.add("down", filter=Condition(menu_active))
    def _(event):
        menu_move(event.current_buffer.text, 1)
        event.app.invalidate()

    @kb.add("enter")
    def _(event):
        # 多行编辑：回车提交。命令菜单：执行选中项；@ 文件补全：只接受路径。
        buf = event.current_buffer
        selected = menu_selected(buf.text)
        if selected:
            buf.text = selected
            buf.cursor_position = len(selected)
            buf.validate_and_handle()
            return
        if not submit_empty and not buf.text.strip():
            return  # 主输入框：空内容回车不提交、不闪屏
        state = buf.complete_state
        if state and state.completions and _at_fragment(buf.text_before_cursor):
            buf.apply_completion(state.current_completion or state.completions[0])
            buf.cancel_completion()
            return
        if state and state.completions:
            buf.apply_completion(state.current_completion or state.completions[0])
        buf.validate_and_handle()

    @kb.add("c-j")
    def _(event):
        event.current_buffer.insert_text("\n")

    @kb.add("escape", "enter")
    def _(event):
        event.current_buffer.insert_text("\n")

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

    @kb.add("c-y")
    def _(event):
        # Ctrl+R 交还给 prompt_toolkit 的历史搜索；重发改到 Ctrl+Y
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


def read_line(
    reader: KeyReader,
    history: list[str],
    default: str = "",
    status: str = "",
    submit_empty: bool = True,
) -> str:
    """prompt_toolkit 行编辑；Ctrl+C/Esc/D/R/O 抛对应异常（与旧语义兼容）。

    default：预填文本（/rewind 后把被回退的用户消息放回输入框）。
    status：输入区状态行（模型/工具/会话/上下文），空串则只显示提示符；
    与对话历史之间用全宽分隔线隔开（类 Claude Code 输入框分区）。
    """
    # 非 TTY（管道/IDE 终端/测试）走 legacy：prompt_toolkit 需要真实终端，
    # 管道输入下会挂起等待；ZHIWEN_INPUT=legacy 可强制旧实现（排查终端兼容）
    if not sys.stdin.isatty():
        return _read_line_legacy(reader, history, default)
    if os.environ.get("ZHIWEN_INPUT") == "legacy":
        return _read_line_legacy(reader, history, default, status, submit_empty)
    try:
        session = make_prompt_session(history, submit_empty)
    except Exception:
        # prompt_toolkit 不可用（极老环境/非 Windows 控制台）时回退旧实现
        _FALLBACK_NOTICE[0] = True
        return _read_line_legacy(reader, history, default, status, submit_empty)
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
                ("class:status", " " + clip(status, max(10, cols - 2)) + "\n"),
                ("class:prompt", "› "),
            ]
        )

    # 命令菜单/底栏配色随主题走（浅色终端用浅底，避免深色块突兀）
    if THEME == "light":
        chrome = {
            "bottom-toolbar": "noreverse bg:#eeeeee fg:#666666",
            ".tbkey": "fg:ansiblue bold",
            ".tb": "fg:#444444",
            ".menu": "fg:#444444",
            ".menu.current": "bg:#dbe7ff fg:#111111 bold",
            ".menu.hint": "fg:#777777",
            "completion-menu.completion": "bg:#f4f4f4 fg:#333333",
            "completion-menu.completion.current": "bg:#dbe7ff fg:#111111",
            "completion-menu.meta.completion": "bg:#f4f4f4 fg:#777777",
            "completion-menu.meta.completion.current": "bg:#dbe7ff fg:#111111",
        }
    else:
        chrome = {
            "bottom-toolbar": "noreverse bg:#262626 fg:#9e9e9e",
            ".tbkey": "fg:ansicyan bold",
            ".tb": "fg:#c8c8c8",
            ".menu": "fg:#c8c8c8",
            ".menu.current": "bg:#3a3a3a fg:#ffffff bold",
            ".menu.hint": "fg:#8a8a8a",
            "completion-menu.completion": "bg:#262626 fg:#c8c8c8",
            "completion-menu.completion.current": "bg:#3a3a3a fg:#ffffff",
            "completion-menu.meta.completion": "bg:#262626 fg:#8a8a8a",
            "completion-menu.meta.completion.current": "bg:#3a3a3a fg:#ffffff",
        }

    # prompt_toolkit 接管 stdin 期间暂停 KeyReader，避免两个读取者抢键
    reader.pause()
    try:
        text = session.prompt(
            message,
            style=Style.from_dict(
                {
                    "prompt": "ansigreen bold",
                    "status": "ansibrightblack",
                    **chrome,
                }
            ),
            multiline=True,
            wrap_lines=True,
            complete_while_typing=True,
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
    reader: KeyReader,
    history: list[str],
    default: str = "",
    status: str = "",
    submit_empty: bool = True,
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
    if _FALLBACK_NOTICE[0]:
        _FALLBACK_NOTICE[0] = False
        print(
            paint(
                "输入：prompt_toolkit 不可用，已切换 legacy 模式（/ 菜单仍可用；"
                "可设 ZHIWEN_INPUT=legacy 固定此模式）",
                "dim",
            )
        )
    if status:
        cols = term_width()
        sys.stdout.write(paint("─" * cols, "dim") + "\n")
        sys.stdout.write(paint(" " + clip(status, max(10, cols - 2)), "dim") + "\n")
    _INPUT_ROWS[0] = 1
    _CURSOR_ROW[0] = 0
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
            elif key.name == "up" and menu_matches(buf):
                menu_move(buf, -1)
            elif key.name == "down" and menu_matches(buf):
                menu_move(buf, 1)
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
                _CURSOR_ROW[0] = 0
            elif key.name == "ctrl-o":
                raise OutputRequested()
            elif key.name in ("ctrl-r", "ctrl-y"):
                raise RetryRequested()
            elif key.name == "enter":
                if not submit_empty and not buf.strip():
                    continue  # 主输入框：空内容回车不提交
                selected = menu_selected(buf)
                if selected:
                    buf = selected
                    pos = len(buf)
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
