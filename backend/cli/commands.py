"""TUI 命令实现：shell 直通、模型切换、会话选择/导出、@ 附件解析。

批次 2 从 cli_agent.py 拆出（2026-11-09），主入口保持在行数棘轮内。
单向依赖：term / format / http / input。
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from cli.format import fmt_num
from cli.http import (
    fetch_conversation_titles,
    fetch_conversations,
    http_json,
    upload_attachment,
)
from cli.input import (
    Interrupted,
    OutputRequested,
    QuitRequested,
    RetryRequested,
    read_line,
)
from cli.term import clip, paint, safe_text, term_width

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMAGES = 6
MAX_ATTACHMENTS = 5
MAX_IMAGE_BYTES = 5 * 1024 * 1024

_MENTION_RE = re.compile(r'(?<![\w@])@(?:"([^"]+)"|\'([^\']+)\'|(\S+))')
_TRAILING = "，。；：、,;:）)"


def run_shell(command: str, reader=None) -> None:
    """!命令：在本机 shell 执行（用户显式触发，不经过模型/审批）。

    reader 非空时临时暂停键盘读取，避免后台 KeyReader 抢走交互式命令的按键。
    """
    command = command.strip()
    if not command:
        print(paint("用法：!<命令>", "yellow"))
        return
    print(paint(f"$ {command}", "dim"), flush=True)
    if reader is not None:
        reader.pause()
    try:
        code = subprocess.run(command, shell=True, timeout=300).returncode
    except KeyboardInterrupt:
        print(paint("已中断命令。", "yellow"))
        return
    except subprocess.TimeoutExpired:
        print(paint("命令超时（300s），已结束等待。", "red"))
        return
    except Exception as exc:  # noqa: BLE001 - 展示给用户即可
        print(paint(f"命令执行失败：{exc}", "red"))
        return
    finally:
        if reader is not None:
            reader.resume()
    if code:
        print(paint(f"[退出码 {code}]", "dim"))


def _ask_index(reader, total: int) -> int | None:
    try:
        sel = read_line(reader, []).strip()
    except (Interrupted, QuitRequested, OutputRequested, RetryRequested):
        return None
    if not sel.isdigit():
        return None
    index = int(sel) - 1
    return index if 0 <= index < total else None


def switch_model(base_url: str, arg: str, reader) -> tuple[str | None, str]:
    """切换对话供应商；返回 (新模型标签, 提示消息)。"""
    try:
        data = http_json(base_url, "GET", "/api/settings", timeout=10) or {}
    except Exception as exc:  # noqa: BLE001
        return None, f"读取供应商失败：{exc}"
    providers = [
        p
        for p in (data.get("providers") or [])
        if p.get("type", "chat") == "chat" and p.get("enabled", True)
    ]
    if not providers:
        return None, "没有可用的对话供应商。"

    active = str(data.get("active_chat_provider") or "")
    target = None
    if arg.isdigit() and 1 <= int(arg) <= len(providers):
        target = providers[int(arg) - 1]
    elif arg:
        needle = arg.lower()
        for p in providers:
            hay = " ".join(
                str(p.get(k) or "") for k in ("id", "name", "model")
            ).lower()
            if needle in hay:
                target = p
                break
        if target is None:
            return None, f"没有匹配「{arg}」的对话供应商。"
    else:
        print(paint("对话供应商（输入序号切换，回车取消）：", "cyan", bold=True))
        for i, p in enumerate(providers, 1):
            mark = "●" if str(p.get("id")) == active else "○"
            label = p.get("name") or p.get("id")
            print(
                paint(f"  {i}. {mark} {label}", "dim")
                + paint(f" · {p.get('model') or ''}", "dim")
            )
        index = _ask_index(reader, len(providers))
        if index is None:
            return None, ""
        target = providers[index]

    pid = str(target.get("id") or "")
    if not pid:
        return None, "该供应商缺少 id，无法切换。"
    if pid == active:
        return str(target.get("model") or pid), ""
    try:
        http_json(
            base_url,
            "POST",
            "/api/settings/switch-chat-provider",
            {"provider_id": pid},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"切换失败：{exc}"
    label = str(target.get("model") or target.get("name") or pid)
    return label, f"已切换对话供应商：{target.get('name') or pid} · {label}"


def pick_conversation(base_url: str, reader, arg: str = "") -> int | None:
    """列出服务端会话并让用户选择；arg 支持序号或标题关键词。"""
    try:
        convs = fetch_conversations(base_url)
    except Exception as exc:  # noqa: BLE001
        print(paint(f"读取会话列表失败：{exc}", "red"))
        return None
    if not convs:
        print(paint("还没有历史会话。", "yellow"))
        return None

    keyword = "" if arg.isdigit() else arg.strip().lower()
    if keyword:
        convs = [
            c
            for c in convs
            if keyword in str(c.get("title") or "").lower()
            or keyword in str(c.get("id"))
        ]
        if not convs:
            print(paint(f"没有标题包含「{arg}」的会话。", "yellow"))
            return None
    convs = convs[:20]

    index = None
    if arg.isdigit() and 1 <= int(arg) <= len(convs):
        index = int(arg) - 1
    else:
        print(paint("最近会话（输入序号恢复，回车取消）：", "cyan", bold=True))
        for i, c in enumerate(convs, 1):
            title = str(c.get("title") or "新对话")
            count = int(c.get("message_count") or 0)
            print(
                paint(f"  {i}. {clip(safe_text(title), term_width() - 24)}", "dim")
                + paint(f" · #{c.get('id')} · {count} 条", "dim")
            )
        index = _ask_index(reader, len(convs))
    if index is None:
        print(paint("已取消恢复。", "dim"))
        return None
    return int(convs[index].get("id"))


def _fmt_ts(value) -> str:
    try:
        return datetime.fromisoformat(str(value)).astimezone().strftime(
            "%m-%d %H:%M:%S"
        )
    except Exception:
        return str(value or "")[:19]


def _mcp_summary(data: dict) -> str:
    servers = data.get("servers") if isinstance(data, dict) else []
    servers = servers or []
    enabled = [s for s in servers if s.get("enabled", True)]
    names = "、".join(
        str(s.get("name") or s.get("id") or "") for s in enabled[:6]
    )
    return f"{len(enabled)}/{len(servers)} 启用" + (f" · {names}" if names else "")


def doctor(base_url: str) -> None:
    """环境诊断：后端 / 索引 / 会话库 / 视觉 / MCP / 技能。"""
    print(paint("环境诊断", "cyan", bold=True))

    def check(name: str, path: str, fmt) -> None:
        try:
            data = http_json(base_url, "GET", path, timeout=30)
            print(
                paint(f"  ✔ {name}", "green")
                + paint(f"  {fmt(data)}", "dim")
            )
        except Exception as exc:  # noqa: BLE001
            print(paint(f"  ✖ {name}  {exc}", "red"))

    check(
        "后端",
        "/api/health",
        lambda d: (
            f"{d.get('status')} · v{d.get('version')} · uptime "
            f"{int(d.get('uptime_seconds') or 0)}s · 单实例锁 "
            f"{'held' if (d.get('instance_lock') or {}).get('held') else 'off'}"
        ),
    )
    check(
        "索引",
        "/api/index/status",
        lambda d: (
            f"{d.get('index_name')} · 文档 {fmt_num(d.get('doc_count') or 0)}"
            f" · {d.get('error') or '无错误'}"
        ),
    )
    check(
        "会话库",
        "/api/conversations",
        lambda d: f"{len(d) if isinstance(d, list) else '?'} 个会话",
    )
    check(
        "视觉",
        "/api/vision/status",
        lambda d: (
            f"{'已配置' if d.get('configured') else '未配置'}"
            f" · {d.get('model') or '-'}"
            f" · 自动识图 {d.get('auto_describe')}"
        ),
    )
    check("MCP", "/api/mcp", _mcp_summary)
    check("技能", "/api/skills", lambda d: f"{d.get('total', 0)} 个")


def show_mcp(base_url: str, arg: str = "") -> None:
    """列出 MCP 服务器（可按名称/命令过滤）。"""
    try:
        data = http_json(base_url, "GET", "/api/mcp", timeout=15) or {}
    except Exception as exc:  # noqa: BLE001
        print(paint(f"读取 MCP 配置失败：{exc}", "red"))
        return
    servers = data.get("servers") or []
    if not servers:
        print(paint("未配置 MCP 服务器。", "yellow"))
        return
    keyword = (arg or "").strip().lower()
    enabled_count = sum(1 for s in servers if s.get("enabled", True))
    print(
        paint(
            f"MCP 服务器（{enabled_count}/{len(servers)} 启用）",
            "cyan",
            bold=True,
        )
    )
    for s in servers:
        hay = " ".join(
            str(s.get(k) or "") for k in ("id", "name", "command", "url")
        ).lower()
        if keyword and keyword not in hay:
            continue
        enabled = s.get("enabled", True)
        mark = "✔" if enabled else "○"
        detail = (
            " ".join(
                [str(s.get("command") or "")] + [str(a) for a in s.get("args") or []]
            ).strip()
            or str(s.get("url") or "")
        )
        print(
            paint(f"  {mark} {s.get('name') or s.get('id')}", "green" if enabled else "dim")
            + paint(
                f"  [{s.get('type') or ''}] {clip(detail, term_width() - 34)}",
                "dim",
            )
        )


def show_skills(base_url: str, arg: str = "") -> None:
    """列出技能；带关键词时按名称/描述过滤。"""
    try:
        data = http_json(base_url, "GET", "/api/skills", timeout=30) or {}
    except Exception as exc:  # noqa: BLE001
        print(paint(f"读取技能列表失败：{exc}", "red"))
        return
    items = data.get("items") or []
    total = int(data.get("total") or len(items))
    keyword = (arg or "").strip().lower()
    if keyword:
        matches = [
            s
            for s in items
            if keyword
            in (
                str(s.get("name") or "") + " " + str(s.get("description") or "")
            ).lower()
        ]
        print(
            paint(
                f"技能匹配（{len(matches)}/{total}）：", "cyan", bold=True
            )
        )
        for s in matches[:20]:
            desc = clip(str(s.get("description") or ""), term_width() - 30)
            print(
                paint(f"  {s.get('name')}", "green")
                + paint(f"  {desc}", "dim")
            )
        if len(matches) > 20:
            print(paint(f"  …其余 {len(matches) - 20} 个", "dim"))
        return

    counts: dict[str, int] = {}
    for s in items:
        source = str(s.get("source") or "?")
        counts[source] = counts.get(source, 0) + 1
    top = " · ".join(
        f"{k} {v}"
        for k, v in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:6]
    )
    print(paint(f"技能 {total} 个 · {top}", "cyan", bold=True))
    for s in items[:15]:
        desc = clip(str(s.get("description") or ""), term_width() - 30)
        print(
            paint(f"  {s.get('name')}", "green") + paint(f"  {desc}", "dim")
        )
    if total > 15:
        print(paint("  …（/skills 关键词 搜索；* 全部列表见 Web 设置页）", "dim"))


def timeline(
    base_url: str, conversation_id: int | None, reader, arg: str = ""
) -> bool:
    """检查点时间线：列表与回滚（回滚会覆盖当前会话状态，需确认）。"""
    if not conversation_id:
        print(paint("还没有会话，先提问。", "yellow"))
        return False
    try:
        data = http_json(
            base_url,
            "GET",
            f"/api/conversations/{conversation_id}/timeline",
            timeout=20,
        ) or {}
    except Exception as exc:  # noqa: BLE001
        print(paint(f"读取检查点失败：{exc}", "red"))
        return False
    items = data.get("items") or []
    if not items:
        note = data.get("note") or "还没有可用的检查点"
        print(paint(note, "yellow"))
        return False

    show = items[:20]
    print(
        paint(
            f"检查点时间线（共 {len(items)} 个，显示最近 {len(show)} 个）",
            "cyan",
            bold=True,
        )
    )
    for i, cp in enumerate(show, 1):
        plan = cp.get("plan") or []
        extra = (
            f" · 计划 {cp.get('plan_done_count', 0)}/{len(plan)}" if plan else ""
        )
        print(
            paint(
                f"  {i:>2}. {_fmt_ts(cp.get('created_at'))} · "
                f"{cp.get('messages', 0)} 条{extra} · "
                f"{str(cp.get('checkpoint_id') or '')[:8]}",
                "dim",
            )
        )

    index = -1
    if arg.isdigit() and 1 <= int(arg) <= len(show):
        index = int(arg) - 1
    else:
        try:
            sel = read_line(reader, []).strip()
        except (Interrupted, QuitRequested, OutputRequested, RetryRequested):
            sel = ""
        if sel.isdigit() and 1 <= int(sel) <= len(show):
            index = int(sel) - 1
    if index < 0:
        print(paint("已取消。", "dim"))
        return False

    target = show[index]
    cid = str(target.get("checkpoint_id") or "")
    print(
        paint(
            f"回滚到第 {index + 1} 个快照（{target.get('messages', 0)} 条消息）"
            "会覆盖当前会话状态。确认？[y/N] ",
            "yellow",
        ),
        end="",
        flush=True,
    )
    try:
        answer = read_line(reader, []).strip().lower()
    except (Interrupted, QuitRequested, OutputRequested, RetryRequested):
        answer = ""
    if answer not in ("y", "yes", "是"):
        print()
        print(paint("已取消回滚。", "dim"))
        return False
    print()
    try:
        http_json(
            base_url,
            "POST",
            f"/api/conversations/{conversation_id}/timeline/{cid}/rollback",
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        print(paint(f"回滚失败：{exc}", "red"))
        return False
    print(
        paint(
            f"✔ 已回滚到快照 {cid[:8]}（{target.get('messages', 0)} 条消息）。"
            "下一轮从该状态继续。",
            "green",
        )
    )
    return True


def _source_label(item: dict) -> str:
    if item.get("type") == "web":
        return str(item.get("title") or item.get("url") or "")
    src = str(item.get("relative_path") or item.get("source") or "知识库片段")
    page = item.get("page")
    return f"{src} · p.{page}" if page else src


def export_conversation(
    base_url: str, conversation_id: int | None, cwd: str, fmt: str = "md"
) -> None:
    """导出当前会话为 Markdown/JSON（消息接口最多返回最近 200 条）。"""
    if not conversation_id:
        print(paint("还没有会话，先提问。", "yellow"))
        return
    fmt = (fmt or "md").strip().lower()
    try:
        messages = http_json(
            base_url,
            "GET",
            f"/api/conversations/{conversation_id}/messages",
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        print(paint(f"读取会话失败：{exc}", "red"))
        return
    if not isinstance(messages, list):
        messages = []
    title = fetch_conversation_titles(base_url).get(conversation_id) or f"会话 {conversation_id}"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if fmt == "json":
        path = Path(cwd) / f"zhiwen_conv_{conversation_id}_{stamp}.json"
        import json

        path.write_text(
            json.dumps(
                {
                    "conversation_id": conversation_id,
                    "title": title,
                    "exported_at": datetime.now().isoformat(timespec="seconds"),
                    "messages": messages,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    else:
        path = Path(cwd) / f"zhiwen_conv_{conversation_id}_{stamp}.md"
        lines = [
            f"# {title}",
            "",
            f"> 会话 {conversation_id} · 导出 "
            f"{datetime.now().isoformat(timespec='seconds')}",
            "",
        ]
        for msg in messages:
            role = str(msg.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            lines.append("## 用户" if role == "user" else "## 助手")
            lines.append("")
            lines.append(content)
            lines.append("")
            sources = [
                s
                for s in (msg.get("sources") or [])
                if isinstance(s, dict) and _source_label(s)
            ]
            if sources:
                lines.append("来源：")
                for s in sources[:8]:
                    lines.append(f"- {_source_label(s)}")
                lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")

    print(
        paint(
            f"✔ 已导出 {len(messages)} 条消息 → {path}",
            "green",
        )
    )


def expand_file_mentions(
    base_url: str, question: str, cwd: str, quiet: bool = False
) -> tuple[str, list[str], list[str]]:
    """解析 @路径：文档上传为附件，图片转 data URL；返回 (问题, 附件ID, 图片)。

    quiet=True 用于 headless（-p）模式，避免提示混进回答正文。
    """
    attachment_ids: list[str] = []
    images: list[str] = []
    seen: set[str] = set()

    def note(text: str, color: str = "dim") -> None:
        if not quiet:
            print(paint(text, color))

    def replace(match: re.Match) -> str:
        if match.group(3):
            clean = match.group(3).rstrip(_TRAILING)
        else:
            clean = match.group(1) or match.group(2) or ""
        path = Path(os.path.expanduser(clean))
        if not path.is_absolute():
            path = Path(cwd) / path
        if not path.is_file():
            # @someone 之类的非路径提及不提示，避免聊天内容误报
            if any(ch in clean for ch in ("/", "\\")) or "." in clean:
                note(f"⚠ 找不到文件：{clean}", "yellow")
            return match.group(0)
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            return match.group(0)
        seen.add(key)

        suffix = path.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            if len(images) >= MAX_IMAGES:
                note(f"⚠ 图片最多 {MAX_IMAGES} 张，已忽略 {path.name}", "yellow")
                return match.group(0)
            try:
                data = path.read_bytes()
            except OSError as exc:
                note(f"⚠ 读取图片失败：{exc}", "yellow")
                return match.group(0)
            if len(data) > MAX_IMAGE_BYTES:
                note(f"⚠ 图片超过 5MB，已忽略 {path.name}", "yellow")
                return match.group(0)
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            images.append(f"data:{mime};base64,{base64.b64encode(data).decode()}")
            note(f"🖼 已附加图片 {path.name}（{fmt_num(len(data))} B）")
            return match.group(0)

        if len(attachment_ids) >= MAX_ATTACHMENTS:
            note(f"⚠ 附件最多 {MAX_ATTACHMENTS} 个，已忽略 {path.name}", "yellow")
            return match.group(0)
        try:
            meta = upload_attachment(base_url, path)
        except Exception as exc:  # noqa: BLE001
            note(f"⚠ 附加 {path.name} 失败：{exc}", "yellow")
            return match.group(0)
        attachment_ids.append(str(meta.get("id") or ""))
        text = f"📎 已附加 {meta.get('name') or path.name}（{fmt_num(meta.get('chars') or 0)} 字"
        if meta.get("truncated"):
            text += "，已截断"
        text += "）"
        if meta.get("warning"):
            text += f" · {meta['warning']}"
        note(text)
        return match.group(0)

    _MENTION_RE.sub(replace, question)
    return question, attachment_ids, images
