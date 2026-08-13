"""终端版 Agent 客户端：在终端里与智能助手对话（纯标准库，无第三方依赖）。

原理：与 Web 前端共用同一个 /api/agent/stream SSE 接口，只是把事件渲染成终端文本。
这就是"终端里和 agent 对话"的最简形态——后续可升级为 Textual TUI / ACP 桥。

用法（先启动后端）：
    python cli_agent.py
    python cli_agent.py --tool auto
    python cli_agent.py --conversation 12

快捷键：
    /exit 或 /quit  退出
    /new            开启新会话
    /tools <mode>   切换工具模式（auto / knowledge / web / none）
    /help           帮助
    Ctrl+C          停止当前回答（后端会感知断开并收尾）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.request

try:
    import colorama

    colorama.just_fix_windows_console()
except Exception:
    pass

ANSI = {
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def paint(text: str, color: str = "", bold: bool = False) -> str:
    prefix = ANSI.get(color, "") + (ANSI["bold"] if bold else "")
    return f"{prefix}{text}{ANSI['reset']}" if prefix else text


_col = [0]


def print_token(text: str) -> None:
    """流式输出 token，按终端宽度自动换行，不打断文本。"""
    try:
        width = max(40, shutil.get_terminal_size((80, 24)).columns)
    except Exception:
        width = 80
    for ch in text:
        if ch == "\n":
            sys.stdout.write("\n")
            _col[0] = 0
            continue
        if _col[0] >= width - 1:
            sys.stdout.write("\n")
            _col[0] = 0
        sys.stdout.write(ch)
        _col[0] += 1
    sys.stdout.flush()


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
        mark = "✔" if t.get("done") else "○"
        color = "green" if t.get("done") else "dim"
        print(paint(f"  {mark} {t.get('text', '')}", color))


SESSION_FILE = ".myragagent_session.json"


def save_session(cwd: str, conversation_id: int) -> None:
    try:
        with open(os.path.join(cwd, SESSION_FILE), "w", encoding="utf-8") as f:
            json.dump({"conversation_id": conversation_id}, f)
    except Exception:
        pass


def load_session(cwd: str) -> int | None:
    try:
        with open(os.path.join(cwd, SESSION_FILE), encoding="utf-8") as f:
            return int((json.load(f) or {}).get("conversation_id") or 0) or None
    except Exception:
        return None


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


COMMANDS = [
    "/exit", "/quit", "/new", "/tools", "/todos", "/status", "/help",
    "/init", "/resume",
]


def _render_menu(buf: str) -> None:
    """实时重绘输入行与 / 命令候选（Windows 10+ 终端支持 ANSI）。"""
    sys.stdout.write("\r\033[2K" + paint("› ", "green", bold=True) + buf)
    sys.stdout.write("\n\033[2K")
    if buf.startswith("/"):
        matches = [c for c in COMMANDS if c.startswith(buf.split(" ")[0])]
        if matches:
            sys.stdout.write("  " + "  ".join(matches[:6]))
    sys.stdout.write(f"\033[1A\033[{2 + len(buf)}C")
    sys.stdout.flush()


def read_input(prompt: str) -> str:
    """带 / 命令实时菜单的输入；非 Windows 回退普通 input。"""
    if sys.platform != "win32":
        return input(prompt)
    import msvcrt

    sys.stdout.write(paint("› ", "green", bold=True))
    sys.stdout.flush()
    buf = ""
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            sys.stdout.write("\n\n")
            return buf
        if ch == "\x03":
            sys.stdout.write("\n")
            raise KeyboardInterrupt
        if ch in ("\b", "\x7f"):
            if buf:
                buf = buf[:-1]
            _render_menu(buf)
        elif ch == "\t":
            matches = [c for c in COMMANDS if c.startswith(buf.split(" ")[0])]
            if matches:
                buf = matches[0]
                _render_menu(buf)
        elif ch == "\x1b":
            buf = ""
            _render_menu(buf)
        elif ch.isprintable():
            buf += ch
            _render_menu(buf)


def resolve_permission(
    base_url: str,
    request_id: str,
    approve: bool,
    reason: str = "",
    remember_forever: bool = False,
    remember_session: bool = False,
) -> bool:
    """向后端提交批准/拒绝决定。"""
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


def _render_permission(data: dict, base_url: str) -> None:
    """渲染审批请求并等待用户输入决定（数字键选择，Enter 确认）。"""
    print()
    print(paint(f"🔐 等待人工确认：{data.get('summary', '')}", "yellow", bold=True))
    args = data.get("arguments") or {}
    for key, value in args.items():
        print(paint(f"   {key}: {value}", "dim"))
    is_command = data.get("name") in ("bash", "command_tool")
    opts = "   1) 批准   2) 拒绝"
    if is_command:
        opts += "   3) 批准并记住（本会话）   4) 批准并永久记住"
    print(paint(opts, "cyan"))
    while True:
        try:
            answer = input(paint("请选择 [1-4，回车=批准]：", "yellow")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = "2"
        if answer in ("", "1", "3", "4"):
            approve = True
            remember_session = answer == "3"
            remember_forever = answer == "4"
            reason = input(paint("备注（可选，回车跳过）：", "dim")).strip()
            ok = resolve_permission(
                base_url,
                data.get("id", ""),
                approve,
                reason,
                remember_forever=remember_forever,
                remember_session=remember_session,
            )
            print(paint("✔ 已批准，继续执行…", "green") if ok else paint("✘ 提交失败，请检查后端", "red"))
            return
        if answer in ("2", "n", "no"):
            reason = input(paint("拒绝原因（可选，回车跳过）：", "dim")).strip()
            ok = resolve_permission(base_url, data.get("id", ""), False, reason)
            print(paint("✘ 已拒绝", "red") if ok else paint("✘ 提交失败，请检查后端", "red"))
            return


def stream_question(
    base_url: str,
    question: str,
    tool_mode: str,
    conversation_id: int | None,
) -> int:
    """发送问题并渲染 SSE 事件，返回本次会话 id。"""
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
    print(paint("─" * 60, "dim"))
    with urllib.request.urlopen(req, timeout=300) as resp:
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
                conv_id = data.get("conversation_id")
                if conv_id:
                    save_session(os.getcwd(), int(conv_id))
                print(paint(f"[会话 {conv_id}]", "dim"))
            elif name == "status":
                print("\r" + paint(f"⏳ {data.get('text', '')}", "yellow"), end="", flush=True)
            elif name == "plan":
                print("\r" + paint("📋 执行计划", "cyan", bold=True))
                for i, step in enumerate(data.get("steps") or [], 1):
                    print(paint(f"   {i}. {step}", "cyan"))
            elif name == "plan_progress":
                done = data.get("done")
                total = data.get("total")
                if done is not None and total:
                    print("\r" + paint(f"📋 计划进度 {done}/{total}", "dim"))
            elif name == "todos":
                print("\r" + paint("任务清单", "cyan", bold=True))
                for t in data.get("todos") or []:
                    mark = "✔" if t.get("done") else "○"
                    color = "green" if t.get("done") else "dim"
                    print(paint(f"  {mark} {t.get('text', '')}", color))
            elif name == "reasoning":
                print("\r" + paint("💭 已深度思考（摘要）", "cyan", bold=True))
                print(paint(f"   {data.get('summary', '')}", "cyan"))
            elif name == "tool_start":
                print("\r" + paint(f"⚙️ 调用 {data.get('name')}…", "yellow"))
            elif name == "tool_result":
                summary = data.get("summary") or ""
                dur = data.get("duration_ms")
                dur_text = f"（{dur}ms）" if isinstance(dur, (int, float)) else ""
                print(paint(f"   ✔ {summary} {dur_text}", "green"))
            elif name == "permission_request":
                _render_permission(data, base_url)
            elif name == "permission_resolved":
                if data.get("approved"):
                    print(paint("   ▶ 已批准", "green"))
                else:
                    print(paint(f"   ▶ 已拒绝：{data.get('reason', '')}", "red"))
            elif name == "token":
                print_token(data)
            elif name == "title":
                print("\r" + paint(f"[标题：{data.get('title', '')}]", "dim"))
            elif name == "done":
                _col[0] = 0
                print()
            elif name == "error":
                print(paint(f"❌ {data.get('message', '未知错误')}", "red"))
    return conv_id  # type: ignore[return-value]


def main() -> None:
    parser = argparse.ArgumentParser(description="终端版 Agent 客户端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--tool", default="auto", choices=["auto", "knowledge", "web", "none"])
    parser.add_argument("--conversation", type=int, default=None)
    args = parser.parse_args()

    base = f"http://{args.host}:{args.port}"
    tool_mode = args.tool
    conversation_id = args.conversation

    print(paint("个人知识库 RAG 智能助手 · 终端版", "cyan", bold=True))
    print(paint(f"工具模式：{tool_mode}   ·   输入问题开始，/help 查看命令", "dim"))
    if os.path.exists(os.path.join(os.getcwd(), "AGENTS.md")):
        print(paint("已加载本目录 AGENTS.md", "dim"))
    else:
        print(paint("提示：输入 /init 可在本目录创建 AGENTS.md（项目记忆）", "dim"))

    while True:
        try:
            question = read_input("› ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        if question.startswith("/"):
            cmd, _, arg = question.partition(" ")
            arg = arg.strip()
            if cmd in ("/exit", "/quit"):
                break
            if cmd == "/new":
                conversation_id = None
                try:
                    os.remove(os.path.join(os.getcwd(), SESSION_FILE))
                except OSError:
                    pass
                print(paint("已开启新会话。", "dim"))
                continue
            if cmd == "/resume":
                resumed = load_session(os.getcwd())
                if resumed:
                    conversation_id = resumed
                    print(paint(f"已恢复会话 {resumed}。", "dim"))
                else:
                    print(paint("本目录还没有可恢复的会话。", "yellow"))
                continue
            if cmd == "/init":
                init_project_memory(os.getcwd())
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
            if cmd == "/status":
                print(
                    paint(
                        f"模式={tool_mode} · 会话={conversation_id or '新会话'}",
                        "dim",
                    )
                )
                continue
            if cmd == "/help":
                print(
                    paint(
                        "/exit /quit 退出 · /new 新会话 · /tools auto|knowledge|web|none\n"
                        "/todos 任务清单 · /status 状态 · /init 创建 AGENTS.md\n"
                        "/resume 恢复本目录上次会话",
                        "dim",
                    )
                )
                continue
            print(paint(f"未知命令：{cmd}（/help 查看）", "yellow"))
            continue
        try:
            conversation_id = stream_question(base, question, tool_mode, conversation_id)
        except KeyboardInterrupt:
            print(paint("\n[已停止生成]", "yellow"))
        except Exception as exc:
            print(paint(f"请求失败：{exc}", "red"))


if __name__ == "__main__":
    sys.exit(main())
