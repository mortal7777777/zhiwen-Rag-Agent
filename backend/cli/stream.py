"""流式问答渲染（自 cli_agent.py 拆出）。

SSE 事件 → 终端渲染 + 打断/挂起恢复/续跑；只依赖下层 cli.* 模块，
由 cli_agent.py（上层入口）导入并保持原有公开名字。
"""

from __future__ import annotations

import collections
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request

from .format import MarkdownStream, fmt_dur, fmt_tokens, todo_mark
from .http import (
    _cancel_run,
    context_pct,
    fetch_context_stats,
    fetch_pending,
    fetch_run_metrics,
    fetch_usage,
    http_error_detail,
    save_session,
)
from .input import KeyReader
from .pager import page_text
from .pending import collect_suspended_decisions
from .render import (
    ask_permission_decision,
    ask_plan_decision,
    ask_question_answer,
    source_lines,
)
from .term import (
    Screen,
    clip,
    notify,
    paint,
    safe_text,
    set_title,
    strwidth,
    term_width,
    visible_rows,
)


# 本会话的工具完整输出（供 Ctrl+O / /output 展开查看）：最新在前访问
TOOL_OUTPUTS: collections.deque = collections.deque(maxlen=30)


def record_tool_output(name: str, summary: str, detail: str) -> None:
    TOOL_OUTPUTS.append({"name": name, "summary": summary, "detail": detail or summary or ""})


def show_tool_output(n: int = 1, reader: KeyReader | None = None) -> None:
    """分页展开最近第 n 次（默认 1）工具调用的完整输出（Ctrl+O / /output）。"""
    if not TOOL_OUTPUTS:
        print(paint("本会话还没有工具输出。", "yellow"))
        return
    if not 1 <= n <= len(TOOL_OUTPUTS):
        print(paint(f"只有 {len(TOOL_OUTPUTS)} 条工具输出，序号需在 1~{len(TOOL_OUTPUTS)} 之间。", "yellow"))
        return
    item = list(TOOL_OUTPUTS)[-n]
    idx = len(TOOL_OUTPUTS) - n + 1
    title = (
        f"⏺ {item['name']}（第 {idx}/{len(TOOL_OUTPUTS)} 条）"
        f"  {item['summary'] or ''}"
    )
    page_text(item["detail"], title, reader)


# ------------------------------------------------------------------ 流式问答

SPINNER = "|/-\\"

# 状态文案：相位 → 同义短句，按已持续秒数轮换（对齐 Claude Code 状态条）。
# 「快好了」只在 finalizing 相位出现，思考阶段不假装快完成。
STATUS_COPY: dict[str, tuple[str, ...]] = {
    "prepare": ("正在准备上下文…", "读取会话与记忆…", "整理工具与文档清单…"),
    "thinking": ("正在思考…", "推敲中…", "核对依据中…", "组织回答中…"),
    "reasoning": ("思考中…", "深入推敲中…", "检查关键依据…"),
    "answering": ("正在生成回答…", "整理措辞中…", "补充细节中…"),
    "finalizing": ("正在核对来源与引用…", "快好了…", "整理最终答复…"),
    "waiting": ("等待你的确认…", "已挂起，等待审批…"),
    "subagent": ("并行处理子任务…", "子代理执行中…", "汇合子任务结果…"),
    "resume": ("正在继续…", "恢复上下文…", "继续执行…"),
    "retry": ("上一步没有产出，正在重试…", "调整思路后重试…", "换个方式再试一次…"),
}


def _activity_copy(phase: str, detail: str, elapsed: int) -> str:
    """按相位挑选轮换文案；unknown 相位回退到事件自带文本。"""
    if phase == "tool" and detail:
        return detail
    items = STATUS_COPY.get(phase or "")
    if not items:
        return detail or "生成中…"
    label = items[max(0, elapsed) // 8 % len(items)]
    if phase == "reasoning" and detail:
        return f"{label} {detail}"
    return label


class GenerationInterrupted(Exception):
    pass


def _process_note_line(text: str) -> str | None:
    """工具轮撤回的过渡文本 → 一行 dim 过程说明（空文本返回 None）。"""
    one = " ".join(str(text or "").split())
    if not one:
        return None
    return f"   · 过程说明：{clip(one, max(10, term_width() - 14))}"


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
    attachment_ids: list[str] | None = None,
    images: list[str] | None = None,
) -> int:
    """发送问题并渲染 SSE 事件；Ctrl+C/Esc 即时打断并通知后端取消。

    continue_run=True：不发问题，改为续跑该会话被中断的一轮（/agent/continue）。
    attachment_ids/images：@ 文件与图片附件，仅随本轮请求携带。
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
                "attachment_ids": attachment_ids or [],
                "images": images or [],
            },
            ensure_ascii=False,
        ).encode("utf-8")
    lines_q: collections.deque = collections.deque()
    lines_cond = threading.Condition()
    resp_holder: list = []
    done_flag = threading.Event()

    def _start_reader(url: str, payload: bytes | None) -> None:
        """开一条 SSE 连接，把逐行数据泵进 lines_q（挂起恢复/续跑要开第二条流）。"""
        from cli.http import auth_headers

        headers = {"Content-Type": "application/json"}
        headers.update(auth_headers())
        req = urllib.request.Request(
            url,
            data=payload,
            headers=headers,
            method="POST",
        )

        def reader_thread() -> None:
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(req, timeout=300) as resp:
                        resp_holder.append(resp)
                        for raw in resp:
                            if done_flag.is_set():
                                break
                            with lines_cond:
                                lines_q.append(raw.decode("utf-8", errors="replace"))
                                lines_cond.notify()
                    break
                except urllib.error.HTTPError as exc:
                    message = http_error_detail(exc)
                    # done 后立刻追问时，服务端可能还在释放会话锁（毫秒级）：
                    # 只对「已有正在进行的回复」做短退避重试，其他 409 直接报。
                    if (
                        exc.code == 409
                        and "正在进行" in message
                        and attempt < 3
                        and not done_flag.is_set()
                    ):
                        time.sleep(0.3 * (attempt + 1))
                        continue
                    with lines_cond:
                        lines_q.append(("__error__", message))
                        lines_cond.notify()
                    break
                except Exception as exc:
                    with lines_cond:
                        lines_q.append(("__error__", str(exc)))
                        lines_cond.notify()
                    break
            with lines_cond:
                lines_q.append(("__end__", None))
                lines_cond.notify()

        threading.Thread(target=reader_thread, daemon=True).start()

    set_title("知问 · 生成中")
    _start_reader(first_url, body)

    md = MarkdownStream(term_width())
    round_printed = ""  # 本轮已打印到终端的文本（含 ANSI），供整轮撤回
    round_text = ""  # 本轮原始文本（未渲染），工具轮撤回后归档为「过程说明」
    skip_next_token = False  # 正文轮的增量已展示：随后的全文 token 跳过
    run_id: str | None = None
    conv_id: int | None = conversation_id
    suspended: dict | None = None  # run_suspended 载荷：流尾收齐决定后走 /agent/resume
    t0 = time.time()
    active_tools: dict[str, tuple[str, float]] = {}
    busy_text: str | None = None
    busy_phase = ""
    busy_detail = ""
    interrupted = False
    frame = 0
    last_partial = time.time()
    reasoning_parts: list[str] = []
    reasoning_chars = 0
    stream_completed = False
    stale_ends = 0  # 409 恢复时旧 reader 还会补一个 __end__，消费掉避免误报断流

    def _next_item():
        with lines_cond:
            lines_cond.wait_for(lambda: bool(lines_q), 0.12)
            if lines_q:
                return lines_q.popleft()
            return None

    def _finish_suspended(outcome: dict) -> bool:
        """处理挂起决定的结果；返回 True 表示已开新流，调用方应 continue。"""
        if outcome.get("resume_body"):
            set_title("知问 · 生成中")
            _start_reader(
                f"{base_url}/api/agent/resume",
                json.dumps(outcome["resume_body"], ensure_ascii=False).encode(
                    "utf-8"
                ),
            )
            return True
        if outcome.get("auto_done"):
            text = outcome.get("text") or ""
            screen.newline()
            if text:
                screen.write(
                    paint("（审批超时自动取消后，后端已继续完成本轮）", "dim")
                )
                screen.write_stream(md.feed(safe_text(text)))
                tail = md.flush()
                if tail:
                    screen.write_stream(tail)
                screen.newline()
            else:
                screen.write(
                    paint(
                        "审批已超时，后端已自动继续执行并结束；可 /resume 查看。",
                        "yellow",
                    )
                )
        elif outcome.get("interrupted"):
            screen.write(paint("运行已中断；可输入 /continue 续跑。", "yellow"))
        else:
            screen.write(
                paint(
                    "等待后端状态超时；请稍后用 /resume 查看结果。",
                    "yellow",
                )
            )
        return False

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
                        label = _activity_copy(
                            busy_phase, busy_detail, int(now - t0)
                        )
                    status = (
                        f"{SPINNER[frame % 4]} {label} · {fmt_dur(now - t0)} · Esc 打断"
                    )
                    screen.busy_line(paint(clip(status, term_width() - 2), "yellow"))
                    frame += 1
                continue
            if isinstance(item, tuple):
                if item[0] == "__end__" and stale_ends:
                    stale_ends -= 1
                    continue
                if item[0] == "__error__":
                    message = str(item[1])
                    # 会话还有挂起审批时新消息会被 409 拒绝：重新加载审批卡，
                    # 而不是只打印一句用户无法操作的提示。
                    if "待确认的操作" in message and conv_id:
                        pending = fetch_pending(base_url, int(conv_id))
                        if pending.get("pending"):
                            screen.newline()
                            screen.write(
                                paint(
                                    "发现未处理的审批，先处理它"
                                    "（处理完请重新输入刚才的问题，↑ 可找回）：",
                                    "yellow",
                                )
                            )
                            outcome = collect_suspended_decisions(
                                base_url,
                                int(conv_id),
                                {
                                    "conversation_id": int(conv_id),
                                    "ops": pending.get("ops") or [],
                                    "plan": pending.get("plan"),
                                    "questions": pending.get("questions") or [],
                                    "created_at": pending.get("created_at"),
                                },
                                reader,
                                screen,
                                ask_permission_decision,
                                ask_plan_decision,
                                ask_question_answer,
                            )
                            if outcome.get("resume_body"):
                                stale_ends += 1
                            if _finish_suspended(outcome):
                                continue
                            break
                    screen.newline()
                    screen.write(paint(f"✖ 流式连接出错：{message}", "red"))
                    if "上一轮执行被中断" in message:
                        screen.write(
                            paint(
                                "   TUI 操作：输入 /continue 续跑该轮，或 /abandon 丢弃后重新提问。",
                                "yellow",
                            )
                        )
                    elif "待确认的操作" in message:
                        screen.write(
                            paint(
                                "   重新加载审批卡失败；请稍后重试，或用 /abandon 丢弃该轮。",
                                "yellow",
                            )
                        )
                    elif "正在进行" in message:
                        screen.write(
                            paint(
                                "   后端仍在执行，稍等几秒再发送；也可用 /status 查看状态。",
                                "yellow",
                            )
                        )
                    break
                if suspended:
                    # 挂起（敏感操作 / 计划确认）：逐项询问 → 一次提交 → 开第二条流续跑。
                    # 若服务端配置了 permission_timeout，批次可能被自动处理；收集决定期间
                    # 轮询 pending，发现批次已被处理就重新同步，不再提交过期决定。
                    conv = suspended.get("conversation_id") or conv_id
                    outcome = collect_suspended_decisions(
                        base_url,
                        conv,
                        suspended,
                        reader,
                        screen,
                        ask_permission_decision,
                        ask_plan_decision,
                        ask_question_answer,
                    )
                    suspended = None
                    if _finish_suspended(outcome):
                        continue
                    break
                if not stream_completed:
                    screen.newline()
                    screen.write(
                        paint(
                            "⚠ 流式连接已断开，未收到完成事件；可 /resume 查看会话结果。",
                            "yellow",
                        )
                    )
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
                busy_phase = safe_text(data.get("phase") or "thinking")
                busy_detail = busy_text
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
                    busy_phase = "plan"
                    busy_detail = f"第 {done}/{total} 步"
                    if data.get("current"):
                        busy_detail += f" · {safe_text(data['current'])}"
            elif name == "todos":
                screen.write(paint("任务清单", "cyan", bold=True))
                for t in data.get("todos") or []:
                    mark = todo_mark(t.get("done"))
                    color = "green" if t.get("done") else "dim"
                    text = clip(safe_text(t.get("text", "")), max(10, term_width() - 6))
                    screen.write(paint(f"   {mark} {text}", color))
            elif name == "reasoning_delta":
                # 思考链增量：不把原文刷进对话区，只用 spinner 显示字数，
                # 完成后可从 /output 展开查看（避免长思考淹没正文）。
                chunk = safe_text(data if isinstance(data, str) else str(data))
                if chunk:
                    reasoning_parts.append(chunk)
                    reasoning_chars += len(chunk)
                    busy_text = f"思考中… {reasoning_chars} 字"
                    busy_phase = "reasoning"
                    busy_detail = f"{reasoning_chars} 字"
            elif name == "vision":
                descs = data.get("descriptions") or []
                model = safe_text(str(data.get("model") or data.get("provider") or ""))
                screen.write(
                    paint("👁 识图完成" + (f" · {model}" if model else ""), "dim")
                )
                if descs:
                    screen.write(
                        paint("   " + clip(safe_text(str(descs[0])), term_width() - 4), "dim")
                    )
            elif name == "tool_start":
                busy_text = None
                tid = data.get("id") or data.get("name")
                name_text = safe_text(data.get("name", "tool"))
                active_tools[str(tid)] = (name_text, time.time())
                max_w = max(20, term_width() - 2)
                head = clip(f"⏺ {name_text}", max_w)
                args_text = render_tool_args(data.get("arguments"))
                line = paint(head, "cyan", bold=True)
                room = max_w - strwidth(head)
                if args_text and room > 4:
                    line += paint(clip(f"({args_text})", room), "dim")
                screen.write(line)
            elif name == "tool_result":
                tid = str(data.get("id") or "")
                started = active_tools.pop(tid, None)
                dur_ms = data.get("duration_ms")
                if started is None:
                    dur = fmt_dur(float(dur_ms) / 1000) if isinstance(dur_ms, (int, float)) else ""
                else:
                    dur = fmt_dur(time.time() - started[1])
                suffix = f"  ({dur})" if dur else ""
                summary = clip(
                    safe_text(data.get("summary") or ""),
                    max(10, term_width() - 6 - strwidth(suffix)),
                )
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
                    reason = clip(
                        safe_text(data.get("reason", "")), max(10, term_width() - 12)
                    )
                    screen.write(paint(f"   ▶ 已拒绝：{reason}", "red"))
            elif name == "run_suspended":
                # 图在敏感操作处挂起：本流即将结束，决定收集在流尾统一做
                suspended = data
                set_title("知问 · 等待审批")
            elif name == "stream_delta":
                # 正文增量：边生成边渲染（与 Web 端一致）。工具轮的过渡思考
                # 文本随后会被 stream_end{keep:false} 整轮撤回，但归档为过程说明。
                busy_text = None
                raw = safe_text(data if isinstance(data, str) else str(data))
                round_text += raw
                chunk = md.feed(raw)
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
                    round_text = ""
                    # 正文已出完，进入收尾：状态条显示「正在核对来源 / 快好了」
                    busy_phase = "finalizing"
                    busy_detail = ""
                else:
                    # 工具轮过渡文本 / 卡死 / 停止生成：整轮撤回。
                    # 只有 tool_round 且确实擦掉了整块时，才补一行 dim 过程说明
                    # （与 Web 端归档到工具轨迹的体验对齐）；其他原因直接撤回。
                    erased = False
                    if round_printed:
                        rows = visible_rows(round_printed, md.width)
                        height = shutil.get_terminal_size((80, 24)).lines
                        if rows < height - 1:
                            screen.erase_block(rows)
                            erased = True
                    if erased and data.get("reason") == "tool_round":
                        note_line = _process_note_line(round_text)
                        if note_line:
                            screen.write(paint(note_line, "dim"))
                    md.reset()
                    round_printed = ""
                    round_text = ""
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
                stream_completed = True
                rest = md.flush()
                screen.write_stream(rest)
                screen.newline()
                if reasoning_chars:
                    record_tool_output(
                        "思考过程", f"{reasoning_chars} 字", "".join(reasoning_parts)
                    )
                    screen.write(
                        paint(f"💭 已思考 {reasoning_chars} 字（/output 查看）", "dim")
                    )
                for line in source_lines(data.get("sources") or []):
                    screen.write(line)
                run_meta = fetch_run_metrics(base_url, conv_id)
                usage = run_meta.get("token_usage") or fetch_usage(base_url, conv_id)
                # 回答完成后刷新上下文占用（输入区状态行的 meter 数据源）
                stats = fetch_context_stats(base_url, conv_id)
                elapsed = fmt_dur(time.time() - t0)
                bits = [f"会话 {conv_id}", tool_mode, f"耗时 {elapsed}"]
                tok = fmt_tokens(usage)
                if tok:
                    bits.append(tok)
                rate = run_meta.get("cache_hit_rate")
                if rate is not None:
                    bits.append(f"缓存 {rate * 100:.0f}%")
                cost = run_meta.get("estimated_cost_yuan")
                if isinstance(cost, (int, float)):
                    bits.append(f"{cost:.4f} 元")
                if stats:
                    bits.append(f"上下文 {context_pct(stats)}%")
                screen.write(paint("─ " + " · ".join(bits) + " ─", "dim"))
                # 长任务完成提醒（>10s）：系统通知 + 响铃，切走窗口也能知道
                if time.time() - t0 > 10:
                    notify("知问", "回答已完成")
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
        set_title("知问")
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
                round_text = ""
            screen.newline()
            screen.write(paint("⏹ 已打断生成" + ("（后端已停止）" if cancelled else "（已断开连接）"), "yellow"))
            screen.write(paint("   按 ↑ 可找回刚才的问题，回车继续对话。", "dim"))
    return conv_id  # type: ignore[return-value]
