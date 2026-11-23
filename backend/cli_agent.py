"""终端版 Agent 客户端（类 Claude Code CLI 风格，纯标准库，无第三方依赖）。

设计要点（参考 Claude Code 的交互设计）：
- 输入线程与 SSE 读取线程分离，主线程轮询事件队列：
  Ctrl+C / Esc 在生成期间可即时打断（不再被阻塞的 socket 读卡住），
  并调用后端 /api/agent/cancel/{run_id} 让模型立刻收尾；
- 流式 Markdown 渲染：标题 / 列表 / 引用 / 代码块 / 行内代码 / 粗体 / 链接 / 表格；
- 工具调用渲染成卡片（⏺ 调用 … / ⎿ 结果 … 耗时），执行中显示 spinner 与已耗时；
- 权限审批用带边框的数字选项弹窗（1/2/3/4，回车=批准，a=全部批准，s=停止本轮）；
- 状态栏：会话 / 模式 / 模型 / 耗时 / tokens；
- 键盘：↑/↓ 历史，Ctrl+C/Esc 打断，Ctrl+L 清屏，Ctrl+R 重发上一问，
  Ctrl+D 空输入退出，Tab 补全 / 命令，←/→ 光标；
- 历史持久化到目录 .zhiwen_history.json，会话到 .zhiwen_session.json。

用法（先启动后端）：
    python cli_agent.py
    python cli_agent.py --tool auto --conversation 12
"""

from __future__ import annotations

from cli.term import (  # noqa: F401 —— 兼容层：旧代码/测试仍从 cli_agent 取这些名字
    ANSI, USE_COLOR, _ANSI_RE, paint, strip_ansi, safe_text, strwidth, clip, pad_right, wrap_styled, term_width, visible_rows, Screen, notify, set_title,
)
from cli.format import (  # noqa: F401 —— 兼容层：旧代码/测试仍从 cli_agent 取这些名字
    _MD_HR_RE, _MD_HEAD_RE, _MD_QUOTE_RE, _MD_LIST_RE, _MD_FENCE_RE, fmt_dur, fmt_num, fmt_tokens, inline_md, MarkdownStream,
)
from cli.input import (  # noqa: F401 —— 兼容层：旧代码/测试仍从 cli_agent 取这些名字
    COMMANDS, _INPUT_ROWS, _CURSOR_ROW, Key, KeyReader, _common_prefix, input_layout, complete, _render_input, _toolbar_text, make_prompt_session, read_line, _read_line_legacy, Interrupted, QuitRequested, RetryRequested, OutputRequested,
)
from cli.http import (  # noqa: F401 —— 兼容层：旧代码/测试仍从 cli_agent 取这些名字
    LAST_CONTEXT, SESSION_FILE, HISTORY_FILE, http_json, http_error_detail, check_backend, fetch_conversation_titles, fetch_conversation_messages, fetch_model_label, fetch_pending, fetch_todos, fetch_usage, fetch_run_metrics, show_cost, fetch_context_stats, ctx_meter, context_pct, save_session, load_session_history, load_history, save_history, _cancel_run,
)
from cli.pending import collect_suspended_decisions
from cli.render import (  # noqa: F401 —— 兼容层：旧代码/测试仍从 cli_agent 取这些名字
    ask_permission_decision, ask_plan_decision, ask_question_answer,
    boxed as _boxed, permission_rows as _permission_rows, source_lines,
)
from cli.commands import (
    doctor, expand_file_mentions, export_conversation, pick_conversation,
    run_shell, show_mcp, show_skills, switch_model, timeline,
)
from cli.pager import page_text
from cli.stream import (  # noqa: F401 —— 兼容层：旧代码/测试仍从 cli_agent 取这些名字
    SPINNER, GenerationInterrupted, TOOL_OUTPUTS, record_tool_output,
    render_tool_args, show_tool_output, stream_question,
)

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
import urllib.error
import urllib.request

try:
    import colorama

    colorama.just_fix_windows_console()
except Exception:  # pragma: no cover
    pass

# ------------------------------------------------------------------ 基础输出


# ------------------------------------------------------------------ Markdown


# ------------------------------------------------------------------ 输入读取


def _clear_screen() -> None:
    if USE_COLOR:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    _INPUT_ROWS[0] = 1
    _CURSOR_ROW[0] = 0


# ------------------------------------------------------------------ 会话/历史


# ---------------- 输入区状态（模型/上下文 meter，Claude Code 式底栏） ----------------

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
    effective_raw = stats.get("effective_history_messages")
    effective = int(effective_raw) if effective_raw is not None else count
    max_msgs = int(stats.get("history_max_messages") or 1)
    high = int(stats.get("history_high_water_messages") or max_msgs)
    window_tokens = int(stats.get("estimated_tokens") or 0)
    sent = stats.get("sent_estimate_tokens")
    tokens = int(sent if sent is not None else window_tokens)
    budget = int(stats.get("token_budget") or 1)
    trimmed_msgs = int(stats.get("trimmed_messages") or 0)
    trimmed_tokens = int(stats.get("trimmed_tokens") or 0)
    window_msgs = effective + trimmed_msgs
    suffix = f"，会话累计 {count} 条" if count != effective else ""
    if trimmed_msgs:
        suffix += f"，窗口 {window_msgs} 条已裁 {trimmed_msgs}"
    print(paint("上下文占用", "cyan", bold=True))
    print(
        paint(
            f"   消息   {_bar(effective, high)} {effective} 条发给模型"
            f"（上限 {max_msgs}，超 {high} 自动压缩{suffix}）",
            "dim",
        )
    )
    tok_extra = f"，窗口估算 {fmt_num(window_tokens)}" if window_tokens != tokens else ""
    if trimmed_tokens:
        tok_extra += f"，已裁 {fmt_num(trimmed_tokens)}"
    print(
        paint(
            f"   tokens {_bar(tokens, budget)} {fmt_num(tokens)}/{fmt_num(budget)}"
            f"（下一轮发送估算{tok_extra}）",
            "dim",
        )
    )
    source = stats.get("budget_source")
    if source:
        print(paint(f"   预算   {fmt_num(budget)} tokens（{source}）", "dim"))
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
    user = os.path.join(os.path.expanduser("~"), ".zhiwen", "AGENTS.md")
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
# 渲染与等待逻辑在 cli/render.py（批次 1 拆出，主入口保持行数棘轮内）。


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
    question, attachment_ids, images = expand_file_mentions(
        base_url, question, os.getcwd(), quiet=True
    )
    body = json.dumps(
        {
            "question": question,
            "conversation_id": conversation_id,
            "tool_mode": tool_mode,
            "template_id": None,
            "project_dir": os.getcwd(),
            "command_sandbox": sandbox,
            "attachment_ids": attachment_ids,
            "images": images,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    answer: list[str] = []
    sources: list[dict] = []
    tool_trace: list[dict] = []
    conv_id: int | None = conversation_id
    error: str | None = None

    def _consume(url: str, payload: bytes) -> dict | None:
        """消费一条 SSE 流；返回 run_suspended 载荷（无挂起则 None）。"""
        nonlocal conv_id, error
        from cli.http import auth_headers

        suspended = None
        sreq_headers = {"Content-Type": "application/json"}
        sreq_headers.update(auth_headers())
        sreq = urllib.request.Request(url, data=payload, headers=sreq_headers)
        with urllib.request.urlopen(sreq, timeout=600) as resp:
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
                elif name == "run_suspended":
                    suspended = data
                elif name == "error":
                    error = str(data.get("message") or "未知错误")
        return suspended

    try:
        suspended = _consume(f"{base_url}/api/agent/stream", body)
        rounds = 0
        # 无人值守：挂起的敏感操作全部按拒绝处理（零副作用），最多连续 5 轮
        while suspended and rounds < 5 and not error:
            rounds += 1
            body: dict = {
                "conversation_id": suspended.get("conversation_id") or conv_id,
                "decisions": [],
                "stopped": False,
            }
            if suspended.get("plan"):
                # 计划同样不自动执行：拒绝后模型会说明或调整，不会产生副作用
                body["plan"] = {
                    "approved": False,
                    "reason": "非交互模式（-p）：计划未确认",
                }
            suspended = _consume(
                f"{base_url}/api/agent/resume",
                json.dumps(body, ensure_ascii=False).encode("utf-8"),
            )
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
    """圆角欢迎横幅（模型/目录/记忆）+ 分栏命令与快捷键总览（类 Claude Code 启动页）。"""
    rows = [
        paint("模型 ", "dim")
        + paint(model or "未知", "cyan")
        + paint("   工具模式 ", "dim")
        + paint(tool_mode, "cyan"),
        paint("目录 ", "dim") + clip(safe_text(os.getcwd()), max(20, term_width() - 10)),
        paint("记忆 ", "dim")
        + ("已加载 AGENTS.md" if has_agents else paint("未创建（/init 可建）", "dim")),
    ]
    _boxed(rows, "知问 ZhiWen - 终端版", "cyan")
    groups = [
        ("会话", "/new 新会话 · /resume [序号|关键词] 恢复 · /continue 续跑 · /abandon 丢弃 · /clear 清屏 · /exit 退出"),
        ("上下文", "/context 占用 · /compact 压缩 · /rewind 回退 · /todos 任务"),
        ("查看", "/output [n] 展开输出 · /cost 用量 · /status 状态 · /tools 模式"),
        ("模型", "/model [序号|名称] 切换供应商 · /export [md|json] 导出会话"),
        ("诊断", "/timeline 快照回滚 · /doctor 环境诊断 · /mcp · /skills"),
        ("输入", "@路径 附加文件/图片 · !命令 直接执行 · Ctrl+J 换行"),
        ("记忆", "/init 创建 AGENTS.md · /memory 项目记忆"),
    ]
    wname = max(strwidth(name) for name, _ in groups)
    for name, cmds in groups:
        pad = " " * (wname - strwidth(name) + 1)
        prefix = "  " + name + pad
        body = wrap_styled(cmds, max(20, term_width() - strwidth(prefix)))
        for i, line in enumerate(body or [""]):
            head = paint(prefix, "cyan") if i == 0 else " " * strwidth(prefix)
            print(head + paint(line, "dim"))
    quick_prefix = "  快捷 "
    quick = (
        "? 帮助 · Ctrl+J 换行 · Ctrl+O 分页 · Ctrl+R 搜索 · Ctrl+Y 重发 · "
        "Ctrl+L 清屏 · ↑/↓ 历史 · Ctrl+C 打断"
    )
    for i, line in enumerate(
        wrap_styled(quick, max(20, term_width() - strwidth(quick_prefix))) or [""]
    ):
        head = paint(quick_prefix, "cyan") if i == 0 else " " * strwidth(quick_prefix)
        print(head + paint(line, "dim"))


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
    if check_backend(base) is None:
        print()
        print(paint("✖ 后端未启动或不可达：" + base, "red"))
        print("  请先启动依赖服务（MySQL / OpenSearch / 后端）：")
        print("    - 一键启动：仓库根目录运行 start_all.bat（会检查并拉起所需服务）")
        print("    - 只重启后端：backend\\restart_backend.ps1")
        print("    - 启动失败先看 backend\\uvicorn.err.log")
        print()
        raise SystemExit(1)
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
                    submit_empty=False,
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
                show_tool_output(1, reader)
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
            if question.startswith("!"):
                run_shell(question[1:], reader)
                continue
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
                    selected = pick_conversation(base, reader, arg)
                    if selected is None:
                        continue
                    conversation_id = selected
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
                if cmd == "/model":
                    new_model, message = switch_model(base, arg, reader)
                    if message:
                        print(paint(message, "green" if new_model else "yellow"))
                    if new_model:
                        model = new_model
                    continue
                if cmd == "/export":
                    export_conversation(base, conversation_id, cwd, arg or "md")
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
                if cmd == "/doctor":
                    doctor(base)
                    continue
                if cmd == "/mcp":
                    show_mcp(base, arg)
                    continue
                if cmd == "/skills":
                    show_skills(base, arg)
                    continue
                if cmd == "/timeline":
                    if timeline(base, conversation_id, reader, arg):
                        fetch_context_stats(base, conversation_id)  # 消息已重建
                    continue
                if cmd == "/continue":
                    if not conversation_id:
                        print(paint("当前没有会话（先提问，或 /resume 恢复）。", "yellow"))
                        continue
                    try:
                        st = http_json(
                            base,
                            "GET",
                            f"/api/agent/pending/{conversation_id}",
                            timeout=8,
                        ) or {}
                    except urllib.error.HTTPError as exc:
                        print(
                            paint(
                                f"读取会话状态失败：{http_error_detail(exc)}",
                                "red",
                            )
                        )
                        continue
                    except Exception as exc:
                        print(paint(f"读取会话状态失败：{exc}", "red"))
                        continue
                    if not st.get("interrupted"):
                        hint = (
                            "有挂起确认（在对话里直接批准/拒绝即可）"
                            if st.get("pending")
                            else "没有未完成的一轮"
                        )
                        print(paint(f"/continue 只用于续跑被中断的一轮：{hint}。", "yellow"))
                        continue
                    conversation_id = stream_question(
                        base,
                        "",
                        tool_mode,
                        conversation_id,
                        reader,
                        screen,
                        sandbox_mode,
                        continue_run=True,
                    )
                    continue
                if cmd == "/abandon":
                    if not conversation_id:
                        print(paint("当前没有会话。", "yellow"))
                        continue
                    try:
                        res = http_json(
                            base,
                            "POST",
                            f"/api/agent/abandon/{conversation_id}",
                            timeout=10,
                        ) or {}
                    except Exception as exc:
                        print(paint(f"丢弃失败：{exc}", "red"))
                        continue
                    if res.get("ok"):
                        print(
                            paint(
                                "已丢弃未完成的一轮（保留历史回答），可以继续提问。",
                                "green",
                            )
                        )
                    else:
                        print(paint("没有可丢弃的未完成运行。", "yellow"))
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
                    show_tool_output(n, reader)
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
                            "/output [n] 分页查看工具输出 · /sandbox docker|subprocess|off 命令沙箱\n"
                            "/init 创建 AGENTS.md · /memory 项目记忆 · /resume [序号|关键词] 恢复会话\n"
                            "/model [序号|名称] 切换供应商 · /export [md|json] 导出会话\n"
                            "/timeline [序号] 检查点回滚 · /doctor 环境诊断 · /mcp · /skills\n"
                            "/continue 续跑被中断的一轮（进程被杀/异常退出后）\n"
                            "!命令 直接执行 shell · @路径 附加文件/图片 · Ctrl+J 换行\n"
                            "Ctrl+C/Esc 打断 · Ctrl+L 清屏 · Ctrl+R 历史搜索 · Ctrl+Y 重发 · Ctrl+O 分页\n"
                            "↑/↓ 历史\n"
                            "分页内：/ 搜索 · n/N 下一处 · g/G 首尾 · q 返回\n"
                            "环境变量：NO_COLOR=1 关色 · ZHIWEN_THEME=dark|light · ZHIWEN_ASCII=1\n"
                            "          ZHIWEN_PAGER=0 关分页 · ZHIWEN_INPUT=legacy 强制旧输入（排查终端兼容）",
                            "dim",
                        )
                    )
                    continue
                print(paint(f"未知命令：{cmd}（/help 查看）", "yellow"))
                continue
            last_question = question
            question, attachment_ids, images = expand_file_mentions(
                base, question, cwd
            )
            conversation_id = stream_question(
                base,
                question,
                tool_mode,
                conversation_id,
                reader,
                screen,
                sandbox_mode,
                attachment_ids=attachment_ids,
                images=images,
            )
    except KeyboardInterrupt:
        print()
    finally:
        reader.stop()
        save_history(cwd, history)
        print(paint("再见。", "dim"))


if __name__ == "__main__":
    sys.exit(main())
