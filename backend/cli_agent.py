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
- 历史持久化到目录 .zhiwen_history.json，会话到 .zhiwen_session.json。

用法（先启动后端）：
    python cli_agent.py
    python cli_agent.py --tool auto --conversation 12
"""

from __future__ import annotations

from cli.term import (  # noqa: F401 —— 兼容层：旧代码/测试仍从 cli_agent 取这些名字
    ANSI, USE_COLOR, _ANSI_RE, paint, strip_ansi, safe_text, strwidth, clip, pad_right, wrap_styled, term_width, visible_rows, Screen,
)
from cli.format import (  # noqa: F401 —— 兼容层：旧代码/测试仍从 cli_agent 取这些名字
    _MD_HR_RE, _MD_HEAD_RE, _MD_QUOTE_RE, _MD_LIST_RE, _MD_FENCE_RE, fmt_dur, fmt_num, fmt_tokens, inline_md, MarkdownStream,
)
from cli.input import (  # noqa: F401 —— 兼容层：旧代码/测试仍从 cli_agent 取这些名字
    COMMANDS, _INPUT_ROWS, Key, KeyReader, _common_prefix, input_layout, complete, _render_input, _toolbar_text, make_prompt_session, read_line, _read_line_legacy, Interrupted, QuitRequested, RetryRequested, OutputRequested,
)
from cli.http import (  # noqa: F401 —— 兼容层：旧代码/测试仍从 cli_agent 取这些名字
    LAST_CONTEXT, SESSION_FILE, HISTORY_FILE, http_json, fetch_conversation_titles, fetch_conversation_messages, fetch_model_label, fetch_todos, fetch_usage, show_cost, fetch_context_stats, ctx_meter, context_pct, save_session, load_session_history, load_history, save_history, _cancel_run,
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
    effective = int(stats.get("effective_history_messages") or count)
    max_msgs = int(stats.get("history_max_messages") or 1)
    high = int(stats.get("history_high_water_messages") or max_msgs)
    tokens = int(stats.get("estimated_tokens") or 0)
    budget = int(stats.get("token_budget") or 1)
    suffix = f"，会话累计 {count} 条" if count != effective else ""
    print(paint("上下文占用", "cyan", bold=True))
    print(
        paint(
            f"   消息   {_bar(effective, high)} {effective} 条发给模型"
            f"（上限 {max_msgs}，超 {high} 自动压缩{suffix}）",
            "dim",
        )
    )
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


def _boxed(rows: list[str], title: str, color: str) -> None:
    """圆角面板：边框弱化（dim）、标题高亮、内容保持自身着色。

    欢迎横幅 / 审批卡 / 计划确认卡共用，保证三处视觉一致。
    """
    inner = max([strwidth(r) for r in rows] + [strwidth(title) + 4] + [0])
    width = min(term_width() - 2, inner + 2)
    head_pad = max(0, width - strwidth(title) - 4)
    print(
        paint("╭─ ", "dim")
        + paint(title, color, bold=True)
        + paint(" " + "─" * head_pad + "╮", "dim")
    )
    for row in rows:
        pad = " " * max(0, width - strwidth(row) - 1)
        print(paint("│", "dim") + " " + row + pad + paint("│", "dim"))
    print(paint("╰" + "─" * max(0, width - 1) + "╯", "dim"))


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


def ask_permission_decision(reader: KeyReader, data: dict) -> dict:
    """渲染审批弹窗并等待数字键决定（回车=批准，Esc=拒绝）；只收集不提交。

    返回 /agent/resume 的一条决定（call_id 兼容 run_suspended 的 ops 与旧
    permission_request 的 id）。
    """
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
    return {
        "call_id": str(data.get("call_id") or data.get("id") or ""),
        "approve": approve,
        "reason": "" if approve else "用户拒绝了该操作",
        "remember_session": answer == "3" and is_command,
        "remember_forever": answer == "4" and is_command,
    }


def ask_plan_decision(reader: KeyReader, plan: dict) -> dict:
    """渲染计划确认框并等待决定（回车/1=确认执行，Esc/2=拒绝）；只收集不提交。"""
    print()
    rows = [
        clip(f"{i}. {safe_text(s)}", term_width() - 6)
        for i, s in enumerate(plan.get("steps") or [], 1)
    ]
    rows += [paint("  1) 确认执行    2) 拒绝", "cyan")]
    rows += [paint("  确认后同一轮直接开始执行；拒绝则模型可调整后重新提交", "dim")]
    _boxed(rows, safe_text(plan.get("summary") or "执行计划待确认"), "cyan")
    print(paint("选择 [1-2] / 回车=确认 / Esc=拒绝：", "cyan"), end="", flush=True)
    while True:
        key = reader.get(timeout=300)
        if key is None or key.name == "esc":
            answer = "2"
        elif key.name == "enter":
            answer = "1"
        elif key.name == "char" and key.data in ("1", "2"):
            answer = str(key.data)
        else:
            continue
        break
    print()
    return {
        "approved": answer == "1",
        "steps": list(plan.get("steps") or []),
        "reason": "" if answer == "1" else "用户拒绝了该计划",
    }


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


def stream_question(
    base_url: str,
    question: str,
    tool_mode: str,
    conversation_id: int | None,
    reader: KeyReader,
    screen: Screen,
    sandbox: str | None = None,
    continue_run: bool = False,
) -> int:
    """发送问题并渲染 SSE 事件；Ctrl+C/Esc 即时打断并通知后端取消。

    continue_run=True：不发问题，改为续跑该会话被中断的一轮（/agent/continue）。
    """
    if continue_run and conversation_id:
        first_url = f"{base_url}/api/agent/continue/{conversation_id}"
        body = None
    else:
        first_url = f"{base_url}/api/agent/stream"
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
    lines_q: collections.deque = collections.deque()
    lines_cond = threading.Condition()
    resp_holder: list = []
    done_flag = threading.Event()

    def _start_reader(url: str, payload: bytes | None) -> None:
        """开一条 SSE 连接，把逐行数据泵进 lines_q（挂起恢复/续跑要开第二条流）。"""
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

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

    _start_reader(first_url, body)

    md = MarkdownStream(term_width())
    round_printed = ""  # 本轮已打印到终端的文本（含 ANSI），供整轮撤回
    skip_next_token = False  # 正文轮的增量已展示：随后的全文 token 跳过
    run_id: str | None = None
    conv_id: int | None = conversation_id
    suspended: dict | None = None  # run_suspended 载荷：流尾收齐决定后走 /agent/resume
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
                if suspended:
                    # 挂起（敏感操作 / 计划确认）：逐项询问 → 一次提交 → 开第二条流续跑
                    ops = suspended.get("ops") or []
                    plan = suspended.get("plan") or None
                    screen.newline()
                    if plan and ops:
                        label = f"{len(ops)} 项敏感操作 + 执行计划"
                    elif plan:
                        label = "执行计划"
                    else:
                        label = f"{len(ops)} 项敏感操作"
                    screen.write(paint(f"⏸ 等待确认：{label}", "yellow"))
                    decisions = [ask_permission_decision(reader, op) for op in ops]
                    resume_body: dict = {
                        "conversation_id": suspended.get("conversation_id")
                        or conv_id,
                        "decisions": decisions,
                        "stopped": False,
                    }
                    if plan:
                        resume_body["plan"] = ask_plan_decision(reader, plan)
                    suspended = None
                    _start_reader(
                        f"{base_url}/api/agent/resume",
                        json.dumps(resume_body, ensure_ascii=False).encode("utf-8"),
                    )
                    continue
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
            elif name == "permission_resolved":
                if data.get("approved"):
                    screen.write(paint("   ▶ 已批准", "green"))
                else:
                    screen.write(paint(f"   ▶ 已拒绝：{safe_text(data.get('reason', ''))}", "red"))
            elif name == "run_suspended":
                # 图在敏感操作处挂起：本流即将结束，决定收集在流尾统一做
                suspended = data
            elif name == "stream_delta":
                # 正文增量：边生成边渲染（与 Web 端一致）。工具轮的过渡思考
                # 文本随后会被 stream_end{keep:false} 整轮撤回。
                busy_text = None
                chunk = md.feed(
                    safe_text(data if isinstance(data, str) else str(data))
                )
                chunk += md.partial()
                if chunk:
                    screen.write_stream(chunk)
                    round_printed += chunk
                last_partial = time.time()
            elif name == "stream_end":
                if data.get("keep"):
                    # 增量即正文：把末行收尾（避免与后续轮次输出拼在同一行，
                    # 那会让"只撤回本轮"的清屏范围算错），全文 token 跳过
                    tail = md.flush()
                    if tail:
                        screen.write_stream(tail)
                    screen.newline()
                    skip_next_token = True
                    round_printed = ""
                else:
                    # 工具轮过渡文本 / 卡死 / 停止生成：整轮撤回
                    if round_printed:
                        rows = visible_rows(round_printed, md.width)
                        height = shutil.get_terminal_size((80, 24)).lines
                        if rows < height - 1:
                            screen.erase_block(rows)
                    md.reset()
                    round_printed = ""
            elif name == "token":
                if skip_next_token:
                    # 正文已按增量展示；这条全文 token 服务 CLI 之外的消费方
                    skip_next_token = False
                else:
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
            # 打断时本轮正文不落库（与 Web 端一致）：撤回已打印的半截内容
            if round_printed:
                rows = visible_rows(round_printed, md.width)
                height = shutil.get_terminal_size((80, 24)).lines
                if rows < height - 1:
                    screen.erase_block(rows)
                round_printed = ""
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
    answer: list[str] = []
    sources: list[dict] = []
    tool_trace: list[dict] = []
    conv_id: int | None = conversation_id
    error: str | None = None

    def _consume(url: str, payload: bytes) -> dict | None:
        """消费一条 SSE 流；返回 run_suspended 载荷（无挂起则 None）。"""
        nonlocal conv_id, error
        suspended = None
        sreq = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
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
    _boxed(rows, "知问 ZhiWen · 终端版", "cyan")
    groups = [
        ("会话", "/new 新会话 · /resume 恢复 · /continue 续跑中断轮 · /clear 清屏 · /exit 退出"),
        ("上下文", "/context 占用 · /compact 压缩 · /rewind 回退 · /todos 任务"),
        ("查看", "/output [n] 展开输出 · /cost 用量 · /status 状态 · /tools 模式"),
        ("记忆", "/init 创建 AGENTS.md · /memory 项目记忆"),
    ]
    wname = max(strwidth(name) for name, _ in groups)
    for name, cmds in groups:
        pad = " " * (wname - strwidth(name) + 1)
        print(paint("  " + name + pad, "cyan") + paint(cmds, "dim"))
    print(
        paint("  快捷 ", "cyan")
        + paint(
            "? 帮助 · Ctrl+O 展开输出 · Ctrl+R 重发 · Ctrl+L 清屏 · ↑/↓ 历史 · Ctrl+C 打断",
            "dim",
        )
    )


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
                if cmd == "/continue":
                    if not conversation_id:
                        print(paint("当前没有会话（先提问，或 /resume 恢复）。", "yellow"))
                        continue
                    st = http_json(
                        base, "GET", f"/agent/pending/{conversation_id}", timeout=8
                    ) or {}
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
                            "/continue 续跑被中断的一轮（进程被杀/异常退出后）\n"
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
