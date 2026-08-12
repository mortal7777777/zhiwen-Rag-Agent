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
                print(paint(f"[会话 {conv_id}]", "dim"))
            elif name == "status":
                print("\r" + paint(f"⏳ {data.get('text', '')}", "yellow"), end="", flush=True)
            elif name == "plan":
                print("\r" + paint("📋 执行计划", "cyan", bold=True))
                for i, step in enumerate(data.get("steps") or [], 1):
                    print(paint(f"   {i}. {step}", "cyan"))
            elif name == "reasoning":
                print("\r" + paint("💭 已深度思考（摘要）", "cyan", bold=True))
                print(paint(f"   {data.get('summary', '')}", "cyan"))
            elif name == "tool_start":
                print("\r" + paint(f"⚙️ 调用 {data.get('name')}…", "yellow"))
            elif name == "tool_result":
                summary = data.get("summary") or ""
                print(paint(f"   ✔ {summary}", "green"))
            elif name == "permission_request":
                _render_permission(data, base_url)
            elif name == "permission_resolved":
                if data.get("approved"):
                    print(paint("   ▶ 已批准", "green"))
                else:
                    print(paint(f"   ▶ 已拒绝：{data.get('reason', '')}", "red"))
            elif name == "token":
                print(data, end="", flush=True)
            elif name == "title":
                print("\r" + paint(f"[标题：{data.get('title', '')}]", "dim"))
            elif name == "done":
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
    print(paint("输入问题开始对话，/help 查看命令，Ctrl+C 停止当前回答。", "dim"))

    while True:
        try:
            question = input(paint("\n你 > ", "green", bold=True)).strip()
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
                print(paint("已开启新会话。", "dim"))
                continue
            if cmd == "/tools":
                if arg in ("auto", "knowledge", "web", "none"):
                    tool_mode = arg
                    print(paint(f"工具模式已切换为：{tool_mode}", "dim"))
                else:
                    print(paint("用法：/tools auto|knowledge|web|none", "yellow"))
                continue
            if cmd == "/help":
                print(paint("/exit /quit 退出 · /new 新会话 · /tools <mode> 切换工具模式", "dim"))
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
