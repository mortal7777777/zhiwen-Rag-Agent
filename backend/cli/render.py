"""审批卡、计划确认与来源附录渲染（TUI 展示层）。

从 cli_agent.py 拆出（2026-11-09）：审批交互与主循环解耦，主入口保持在
结构测试的行数棘轮内。单向依赖：只依赖 cli.term。
"""

from __future__ import annotations

import json
import sys
import threading

from cli.term import (
    ascii_text,
    clip,
    link,
    notify,
    pad_right,
    paint,
    safe_text,
    set_title,
    strwidth,
    term_width,
    wrap_styled,
)


def boxed(rows: list[str], title: str, color: str) -> None:
    """圆角面板：边框弱化（dim）、标题高亮、内容保持自身着色。

    欢迎横幅 / 审批卡 / 计划确认卡共用。行内换行会被拆成独立边框行，
    避免多行参数把边框撑破。

    宽度口径（所有行必须同为 width 列）：
    正文 = │ + 空格 + 内容 + padding + │ = width
    顶边 = ╭─ + 空格 + 标题 + 空格 + 横线 + ╮ = width
    底边 = ╰ + 横线(width-2) + ╯ = width
    """
    limit = term_width()
    raw_title = str(title).strip()
    title = safe_text(raw_title.splitlines()[0] if raw_title else "确认")
    # 中文终端常把 “·/—” 当全角（歧义宽度）渲染，标题里换成 ASCII 避免顶边错位
    title = title.replace("·", "-").replace("—", "-")
    title = clip(title, max(1, limit - 6))
    parts: list[str] = []
    for row in rows:
        for part in str(row).splitlines() or [""]:
            # 歧义宽度字符在中文终端可能按 2 列渲染；边框内统一换 ASCII
            part = part.replace("…", "...").replace("·", "-").replace("—", "-")
            parts.append(part)

    def measure() -> int:
        return max([strwidth(p) for p in parts] + [strwidth(title) + 2, 1])

    inner = measure()
    if inner + 4 > limit:
        # 超宽：内容裁到 limit-4（右侧至少留 1 列内边距，避免文字贴边框）
        parts = [clip(p, max(1, limit - 4)) for p in parts]
        width = limit
    else:
        # 最宽行右侧也留 1 列内边距
        width = inner + 4

    head_dash = max(0, width - strwidth(title) - 5)
    print(
        paint("╭─ ", "dim")
        + paint(title, color, bold=True)
        + paint(" " + "─" * head_dash + "╮", "dim")
    )
    for part in parts:
        pad = " " * max(0, width - strwidth(part) - 3)
        print(paint("│", "dim") + " " + part + pad + paint("│", "dim"))
    print(paint("╰" + "─" * max(0, width - 2) + "╯", "dim"))


def _diff_rows(diff_lines: list, width: int) -> list[str]:
    """编辑审批的 diff：窄终端逐行红删绿增（块内行号），宽终端左右并排。"""
    rows: list[str] = []
    if width < 110:
        rows.append(paint("──── 变更预览（块内行号） ────", "dim"))
        old_no = new_no = 0
        for d in diff_lines[:60]:
            op = str(d.get("op") or "")
            text = clip(safe_text(str(d.get("text") or "")), width - 10)
            if op == "-":
                old_no += 1
                num = f"{old_no:>4}"
            elif op == "+":
                new_no += 1
                num = f"{new_no:>4}"
            else:
                num = "    "
            if op == "-":
                rows.append(paint(f"{num} - {text}", "red"))
            elif op == "+":
                rows.append(paint(f"{num} + {text}", "green"))
            else:
                rows.append(paint(f"{num}   {text}", "dim"))
        if len(diff_lines) > 60:
            rows.append(paint("      …（diff 已截断）", "dim"))
        return rows

    # 宽终端：按删除块/新增块配对成左右两栏
    rows.append(paint("──── 变更预览（左旧 / 右新，块内行号） ────", "dim"))
    left_w = max(10, (width - 14) // 2)
    num_w = 4
    old_no = new_no = 0
    groups: list[tuple[list[str], list[str], str | None]] = []
    i = 0
    while i < len(diff_lines):
        op = str(diff_lines[i].get("op") or "")
        if op in ("-", "+"):
            dels: list[str] = []
            adds: list[str] = []
            while i < len(diff_lines) and str(diff_lines[i].get("op")) == "-":
                dels.append(str(diff_lines[i].get("text") or ""))
                i += 1
            while i < len(diff_lines) and str(diff_lines[i].get("op")) == "+":
                adds.append(str(diff_lines[i].get("text") or ""))
                i += 1
            groups.append((dels, adds, None))
        else:
            groups.append(([], [], str(diff_lines[i].get("text") or "")))
            i += 1
    for dels, adds, note in groups:
        if note is not None:
            rows.append(paint("    " + clip(safe_text(note), width - 6), "dim"))
            continue
        for j in range(max(len(dels), len(adds))):
            if j < len(dels):
                old_no += 1
                old = pad_right(clip(safe_text(dels[j]), left_w), left_w)
                left = paint(f"{old_no:>{num_w}} ", "dim") + paint(old, "red")
            else:
                left = " " * (num_w + 1 + left_w)
            if j < len(adds):
                new_no += 1
                new = pad_right(clip(safe_text(adds[j]), left_w), left_w)
                right = paint(f"{new_no:>{num_w}} ", "dim") + paint(new, "green")
            else:
                right = ""
            rows.append(left + paint(" │ ", "dim") + right)
    return rows


def permission_rows(data: dict) -> list[str]:
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
        rows.extend(_diff_rows(args["diff_lines"], width))
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
    # 其余工具（bash 等）：逐参数展示（JSON 序列化天然转义换行；长参数换行不截断）
    for k, v in args.items():
        text = f"{k}: {safe_text(json.dumps(v, ensure_ascii=False))}"
        rows.extend(wrap_styled(text, max(10, width - 6)))
    return rows


def _prompt_text(base: str, waited: int, color: str) -> str:
    text = f"已等待 {waited}s · {base}" if waited >= 15 else base
    return paint(text, color)


def _wait_key(
    reader,
    base: str,
    color: str,
    stale_event: threading.Event | None,
    valid,
):
    """等待一次有效按键；每 15 秒在原行刷新已等待时长。

    返回 (key, stale)。stale=True 表示服务端已处理该批次，调用方应重新同步。
    """
    waited = 0
    notified = False
    sys.stdout.write(_prompt_text(base, 0, color))
    sys.stdout.flush()
    while True:
        if stale_event is not None and stale_event.is_set():
            print()
            return None, True
        try:
            key = reader.get(timeout=1)
        except KeyboardInterrupt:
            # 审批卡上按 Ctrl+C：交给调用方按「停止本轮」收尾，
            # 否则挂起批次会留在服务端，下一问撞 409
            print()
            return None, False
        waited += 1
        if key is None:
            if waited == 30 and not notified:
                notify("知问", "审批仍在等待你的决定")
                notified = True
            if waited % 15 == 0:
                sys.stdout.write("\r\033[2K" + _prompt_text(base, waited, color))
                sys.stdout.flush()
            continue
        if valid(key):
            return key, False


def ask_permission_decision(
    reader, data: dict, stale_event: threading.Event | None = None
) -> dict:
    """渲染审批弹窗并等待决定（回车=批准，Esc=拒绝）；只收集不提交。

    返回 /agent/resume 的一条决定，或控制标记：
    - {"__stopped__": True}：Ctrl+C / s，停止本轮；
    - {"__approve_all__": True}：a，批准本批剩余全部操作；
    - {"__stale__": True}：服务端已处理该批次，调用方重新同步。
    """
    set_title("知问 · 等待审批")
    print()
    risk = data.get("risk") == "high"
    rememberable = bool(
        data.get("rememberable")
        if "rememberable" in data
        else data.get("name") in ("bash", "command_tool")
    )
    total = int(data.get("_batch_total") or 1)
    index = int(data.get("_batch_index") or 1)
    multi = total > 1

    rows = permission_rows(data)
    if risk:
        rows.append(
            paint(
                "⚠ 高危命令：" + safe_text(data.get("risk_reason") or "可能造成不可逆破坏"),
                "red",
                bold=True,
            )
        )
    nb = " "  # 选项内用不换行空格：窄终端换行只发生在选项之间
    opts = [f"1){nb}批准", f"2){nb}拒绝"]
    if rememberable:
        opts += [f"3){nb}批准(本会话)", f"4){nb}批准(永久)"]
    if multi:
        opts.append(f"a){nb}全部批准")
    opts.append(f"s){nb}停止本轮")
    wrap_w = max(10, term_width() - 8)
    rows.extend(
        paint("  " + line, "cyan")
        for line in wrap_styled("  ".join(opts), wrap_w)
    )

    title = safe_text(data.get("summary", "等待人工确认"))
    if multi:
        title += f" [{index}/{total}]"
    boxed(rows, title, "red" if risk else "yellow")

    keys = "1-4" if rememberable else "1-2"
    base = f"选择 [{keys}{'/a' if multi else ''}/s] / 回车=批准 / Esc=拒绝 / Ctrl+C=停止："

    allowed = ("1", "2") + (("3", "4") if rememberable else ())
    allowed += (("a", "A") if multi else ()) + ("s", "S")

    def valid(key) -> bool:
        if key.name in ("enter", "esc", "ctrl-c"):
            return True
        if key.name == "char":
            return key.data in allowed
        return False

    key, stale = _wait_key(reader, base, "yellow", stale_event, valid)
    if stale:
        return {"__stale__": True}
    if key is None:  # 审批卡上的 Ctrl+C（KeyboardInterrupt 路径）
        return {"__stopped__": True}
    print()
    if key.name == "ctrl-c" or (key.name == "char" and str(key.data).lower() == "s"):
        return {"__stopped__": True}
    if multi and key.name == "char" and str(key.data).lower() == "a":
        return {"__approve_all__": True}
    if key.name == "enter":
        answer = "1"
    elif key.name == "esc":
        answer = "2"
    else:
        answer = str(key.data)
    approve = answer in ("1", "3", "4")
    return {
        "call_id": str(data.get("call_id") or data.get("id") or ""),
        "approve": approve,
        "reason": "" if approve else "用户拒绝了该操作",
        "remember_session": rememberable and answer == "3",
        "remember_forever": rememberable and answer == "4",
    }


def ask_plan_decision(
    reader, plan: dict, stale_event: threading.Event | None = None
) -> dict:
    """渲染计划确认框并等待决定（回车/1=确认执行，Esc/2=拒绝，s=停止本轮）。"""
    set_title("知问 · 等待审批")
    print()
    wrap_w = max(10, term_width() - 6)
    rows: list[str] = []
    for i, s in enumerate(plan.get("steps") or [], 1):
        rows.extend(wrap_styled(f"{i}. {safe_text(s)}", wrap_w))
    nb = " "
    rows += [
        paint("  " + line, "cyan")
        for line in wrap_styled(
            f"1){nb}确认执行    2){nb}拒绝    s){nb}停止本轮", wrap_w - 2
        )
    ]
    rows += [
        paint("  " + line, "dim")
        for line in wrap_styled(
            "确认后同一轮直接开始执行；拒绝则模型可调整后重新提交", wrap_w - 2
        )
    ]
    boxed(rows, safe_text(plan.get("summary") or "执行计划待确认"), "cyan")
    base = "选择 [1-2/s] / 回车=确认 / Esc=拒绝 / Ctrl+C=停止："

    def valid(key) -> bool:
        if key.name in ("enter", "esc", "ctrl-c"):
            return True
        if key.name == "char":
            return key.data in ("1", "2", "s", "S")
        return False

    key, stale = _wait_key(reader, base, "cyan", stale_event, valid)
    if stale:
        return {"__stale__": True}
    if key is None:  # 计划卡上的 Ctrl+C（KeyboardInterrupt 路径）
        return {"__stopped__": True}
    print()
    if key.name == "ctrl-c" or (key.name == "char" and str(key.data).lower() == "s"):
        return {"__stopped__": True}
    approved = key.name == "enter" or (key.name == "char" and key.data == "1")
    return {
        "approved": approved,
        "steps": list(plan.get("steps") or []),
        "reason": "" if approved else "用户拒绝了该计划",
    }


def ask_question_answer(
    reader, data: dict, stale_event: threading.Event | None = None
) -> dict:
    """渲染一个选择题并等待回答（单选/多选/自定义/跳过），只收集不提交。

    交互与 Web 端问答卡一致：↑↓/←→ 移动高亮游标，数字键直选，空格切换多选，
    Enter 确认当前项；「其他（自己输入）」与「跳过此题」也在选项列表内，
    可一路用方向键到达。多选可用空格/数字键累积后再 Enter。
    """
    set_title("知问 · 等待回答")
    print()
    header = safe_text(str(data.get("header") or "").strip())
    question = safe_text(str(data.get("question") or "").strip())
    options = [o for o in (data.get("options") or []) if isinstance(o, dict)]
    multi = bool(data.get("multi_select"))
    width = term_width()

    rows = [
        paint(("❓ " if i == 0 else "   ") + line, "cyan", bold=True)
        for i, line in enumerate(wrap_styled(question, max(20, width - 10)) or [""])
    ]
    rows.extend(
        paint("  " + line, "dim")
        for line in wrap_styled(
            ("可多选" if multi else "单选")
            + " · ↑↓ 移动 · 数字键直选 · 空格切换 · Enter 确认",
            max(10, width - 8),
        )
    )
    boxed(rows, header or "请回答", "cyan")

    selected: list[str] = []
    other = ""
    cursor = 0
    editing = False
    buf = ""
    prev_other = ""
    other_idx = len(options)
    total = len(options) + 2

    def row_text(idx: int) -> tuple[str, bool]:
        """返回（行文本，是否已选/已填）。"""
        if idx < len(options):
            opt = options[idx]
            label = safe_text(str(opt.get("label") or ""))
            desc = safe_text(str(opt.get("description") or "").strip())
            text = f"{idx + 1}) {label}" + (f" — {desc}" if desc else "")
            return text, label in selected
        if idx == other_idx:
            if editing:
                return f"o) 其他（自己输入）：{buf}▏", bool(buf.strip())
            return "o) 其他（自己输入）" + (f"：{other}" if other else ""), bool(other)
        return "s) 跳过此题", False

    def wrapped_row(
        prefix: str, text: str, color: str = "", bold: bool = False
    ) -> list[str]:
        disp = ascii_text(prefix)
        avail = max(8, width - 2 - strwidth(disp))
        body = wrap_styled(text, avail) or [""]
        lines = [disp + body[0]] + [
            " " * strwidth(disp) + line for line in body[1:]
        ]
        if color:
            return [paint(line, color, bold=bold) for line in lines]
        return lines

    def block_lines() -> list[str]:
        lines: list[str] = []
        for idx in range(total):
            text, chosen = row_text(idx)
            mark = "▶ " if idx == cursor else "  "
            check = "✓ " if chosen else "  "
            color = "cyan" if idx == cursor else ("green" if chosen else "")
            lines.extend(
                wrapped_row(mark + check, text, color, bold=idx == cursor)
            )
        if editing:
            lines.extend(
                wrapped_row(
                    "  ", "输入中：Enter 确认 · Esc 取消 · Backspace 删除", "dim"
                )
            )
        elif multi:
            lines.extend(
                wrapped_row(
                    "  ", "多选用空格/数字键累积，Enter 确认；Esc 跳过此题", "dim"
                )
            )
        else:
            lines.extend(wrapped_row("  ", "Enter 确认当前项；Esc 跳过此题", "dim"))
        return lines

    drawn = 0

    def redraw() -> None:
        nonlocal drawn
        if drawn:
            sys.stdout.write(f"\033[{drawn}A")
        lines = block_lines()
        sys.stdout.write("".join("\r\033[2K" + line + "\n" for line in lines))
        sys.stdout.flush()
        drawn = len(lines)

    redraw()

    while True:
        if stale_event is not None and stale_event.is_set():
            print()
            return {"__stale__": True}
        try:
            key = reader.get(timeout=1)
        except KeyboardInterrupt:
            print()
            return {"__stopped__": True}
        if key is None:
            continue
        if editing:
            if key.name == "ctrl-c":
                print()
                return {"__stopped__": True}
            if key.name == "esc":
                editing = False
                other = prev_other
                redraw()
            elif key.name == "enter":
                editing = False
                other = buf.strip()
                if other:
                    selected.clear()
                cursor = other_idx
                redraw()
            elif key.name == "backspace":
                buf = buf[:-1]
                redraw()
            elif key.name == "char":
                ch = str(key.data or "")
                if ch and len(buf) < 500:
                    buf += ch
                redraw()
            continue
        if key.name == "ctrl-c":
            print()
            return {"__stopped__": True}
        if key.name == "esc":
            print()
            return {"selected": [], "other": "", "skipped": True}
        if key.name in ("up", "left"):
            cursor = (cursor - 1) % total
            redraw()
            continue
        if key.name in ("down", "right"):
            cursor = (cursor + 1) % total
            redraw()
            continue
        if key.name == "enter":
            if cursor < len(options):
                if multi:
                    print()
                    return {
                        "selected": list(selected),
                        "other": other.strip(),
                        "skipped": not selected and not other.strip(),
                    }
                label = str(options[cursor].get("label") or "")
                print()
                return {"selected": [label], "other": other.strip(), "skipped": False}
            if cursor == other_idx:
                if other.strip():
                    print()
                    return {"selected": [], "other": other.strip(), "skipped": False}
                editing = True
                prev_other = other
                buf = other
                redraw()
                continue
            print()
            return {"selected": [], "other": "", "skipped": True}
        if key.name != "char":
            continue
        ch = str(key.data or "")
        if ch.lower() == "s":
            print()
            return {"selected": [], "other": "", "skipped": True}
        if ch.lower() == "o":
            editing = True
            prev_other = other
            buf = other
            redraw()
            continue
        if ch == " ":
            if cursor < len(options):
                label = str(options[cursor].get("label") or "")
                if multi:
                    if label in selected:
                        selected.remove(label)
                    else:
                        selected.append(label)
                else:
                    selected = [label]
                redraw()
            elif cursor == other_idx:
                editing = True
                prev_other = other
                buf = other
                redraw()
            else:
                print()
                return {"selected": [], "other": "", "skipped": True}
            continue
        if ch.isdigit() and 1 <= int(ch) <= len(options):
            idx = int(ch) - 1
            label = str(options[idx].get("label") or "")
            cursor = idx
            if multi:
                if label in selected:
                    selected.remove(label)
                else:
                    selected.append(label)
            else:
                selected = [label]
            redraw()


def source_lines(sources: list, limit: int = 8) -> list[str]:
    """把 done 事件的来源列表渲染成紧凑附录（去重、限条数）。"""
    if not sources:
        return []
    width = term_width()
    lines = [paint(f"来源（{len(sources)}）", "cyan", bold=True)]
    seen: set = set()
    shown = 0
    for item in sources:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web":
            url = safe_text(str(item.get("url") or ""))
            title = safe_text(str(item.get("title") or ""))
            if title and url and url != title:
                label = f"{title} — {url}"
            else:
                label = title or url
            key = ("web", url or title)
        else:
            src = safe_text(str(item.get("relative_path") or item.get("source") or ""))
            page = item.get("page")
            score = item.get("score")
            label = src or "知识库片段"
            if page:
                label += f" · p.{page}"
            if isinstance(score, (int, float)):
                label += f" · {score:.2f}"
            key = ("kb", src, page)
        if not label or key in seen:
            continue
        seen.add(key)
        shown += 1
        body = paint(clip(label, width - 8), "dim")
        if item.get("type") == "web" and url:
            body = link(body, url)
        lines.append(paint(f"  {shown}. ", "dim") + body)
        if shown >= limit:
            break
    rest = len(sources) - shown
    if rest > 0:
        lines.append(paint(f"  …其余 {rest} 条（工具输出含完整片段）", "dim"))
    return lines
