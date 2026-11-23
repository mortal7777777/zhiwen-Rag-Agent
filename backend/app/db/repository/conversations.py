"""会话与消息数据访问。"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from ..models import AppMeta, Conversation, Message
from ..sanitize import sanitize_sources, sanitize_token_usage, truncate_tool_trace
from .common import _json_dumps, _json_loads

logger = logging.getLogger(__name__)


def create_conversation(
    db: Session,
    title: str = "新对话",
    system_prompt: str | None = None,
    template_id: int | None = None,
    project_dir: str | None = None,
) -> Conversation:
    conv = Conversation(
        title=title,
        system_prompt=system_prompt,
        template_id=template_id,
        project_dir=project_dir,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def list_conversations(db: Session, limit: int = 200) -> list[dict]:
    """置顶组最前，组内按最近更新排序，附带消息条数。"""
    rows = db.execute(
        select(
            Conversation,
            func.count(Message.id).label("message_count"),
        )
        .outerjoin(Message, Message.conversation_id == Conversation.id)
        .group_by(Conversation.id)
        .order_by(Conversation.pinned.desc(), Conversation.updated_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": conv.id,
            "title": conv.title,
            "template_id": conv.template_id,
            "project_dir": conv.project_dir,
            "system_prompt": conv.system_prompt,
            "prompt_bundle_hash": conv.prompt_bundle_hash,
            "prompt_locked_at": conv.prompt_locked_at,
            "forked_from_conversation_id": conv.forked_from_conversation_id,
            "forked_from_message_id": conv.forked_from_message_id,
            "pinned": bool(conv.pinned),
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
            "message_count": count,
        }
        for conv, count in rows
    ]


def get_conversation(db: Session, conversation_id: int) -> Conversation | None:
    return db.get(Conversation, conversation_id)


def _like_pattern(query: str) -> str:
    """转义 LIKE 通配符，搜索词按字面量匹配。"""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _snippet(text: str, query: str, radius: int = 40) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    if not flat:
        return ""
    index = flat.lower().find(query.lower())
    if index < 0:
        preview = flat[: radius * 2]
        return preview + ("…" if len(flat) > len(preview) else "")
    start = max(0, index - radius)
    end = min(len(flat), index + len(query) + radius)
    return (
        ("…" if start > 0 else "")
        + flat[start:end]
        + ("…" if end < len(flat) else "")
    )


def _workspace_name(path: str) -> str:
    trimmed = str(path or "").rstrip("\\/")
    index = max(trimmed.rfind("\\"), trimmed.rfind("/"))
    return trimmed[index + 1 :] if index >= 0 else trimmed


def search_conversations(db: Session, query: str, limit: int = 20) -> list[dict]:
    """按标题、工作区目录名、消息正文搜索会话（最近更新优先），带命中数与片段。"""
    text = (query or "").strip()
    if not text:
        return []
    pattern = _like_pattern(text)

    counts = (
        select(Message.conversation_id, func.count(Message.id).label("message_count"))
        .group_by(Message.conversation_id)
        .subquery()
    )
    hits = (
        select(
            Message.conversation_id.label("conversation_id"),
            func.count(Message.id).label("hit_count"),
            func.min(Message.id).label("first_hit_id"),
        )
        .where(Message.role.in_(("user", "assistant")))
        .where(Message.content.like(pattern, escape="\\"))
        .group_by(Message.conversation_id)
        .subquery()
    )
    # 只按目录名（basename）匹配工作区：整条路径里常含工程名，会把同工程
    # 所有会话都带出来（例如路径里的 Python 命中所有会话）。
    workspace_paths = [
        path
        for path in db.execute(
            select(Conversation.project_dir)
            .where(Conversation.project_dir.is_not(None))
            .distinct()
        ).scalars()
        if text.lower() in _workspace_name(path).lower()
    ]
    conditions = [
        Conversation.title.like(pattern, escape="\\"),
        hits.c.conversation_id.is_not(None),
    ]
    if workspace_paths:
        conditions.append(Conversation.project_dir.in_(workspace_paths))
    rows = db.execute(
        select(
            Conversation,
            func.coalesce(counts.c.message_count, 0),
            func.coalesce(hits.c.hit_count, 0),
            hits.c.first_hit_id,
        )
        .outerjoin(counts, counts.c.conversation_id == Conversation.id)
        .outerjoin(hits, hits.c.conversation_id == Conversation.id)
        .where(or_(*conditions))
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(limit)
    ).all()

    out: list[dict] = []
    for conv, message_count, hit_count, first_hit_id in rows:
        snippet = ""
        if first_hit_id is not None:
            content = db.execute(
                select(Message.content).where(Message.id == first_hit_id)
            ).scalar_one_or_none()
            snippet = _snippet(content or "", text)
        out.append(
            {
                "id": conv.id,
                "title": conv.title,
                "template_id": conv.template_id,
                "project_dir": conv.project_dir,
                "system_prompt": conv.system_prompt,
                "prompt_bundle_hash": conv.prompt_bundle_hash,
                "prompt_locked_at": conv.prompt_locked_at,
                "forked_from_conversation_id": conv.forked_from_conversation_id,
                "forked_from_message_id": conv.forked_from_message_id,
                "pinned": bool(conv.pinned),
                "created_at": conv.created_at,
                "updated_at": conv.updated_at,
                "message_count": int(message_count or 0),
                "match_count": int(hit_count or 0),
                "snippet": snippet,
            }
        )
    return out


def rename_conversation(db: Session, conversation_id: int, title: str) -> Conversation | None:
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        return None
    conv.title = title.strip()[:200] or "新对话"
    db.commit()
    db.refresh(conv)
    return conv


def update_conversation(
    db: Session,
    conversation_id: int,
    *,
    title: str | None = None,
    template_id: int | None = None,
    project_dir: str | None = None,
    pinned: bool | None = None,
) -> Conversation | None:
    """更新会话字段（标题 / 绑定的模板 / 工作目录 / 置顶）。template_id=None 表示清空绑定。"""
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        return None
    if title is not None:
        conv.title = title.strip()[:200] or "新对话"
    if pinned is not None:
        conv.pinned = bool(pinned)
    if template_id is not None:
        # 0 = 切回默认模板（清空绑定）；None = 不修改。换模板后清空旧锁，
        # 下一次运行由 resolver 重新锁定新模板的最新版本。
        new_template_id = template_id or None
        if new_template_id != conv.template_id:
            conv.template_id = new_template_id
            conv.prompt_bundle = None
            conv.prompt_bundle_hash = None
            conv.prompt_locked_at = None
    if project_dir is not None:
        conv.project_dir = project_dir.strip()[:500] or None
    db.commit()
    db.refresh(conv)
    return conv


def delete_conversation(db: Session, conversation_id: int) -> bool:
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        return False
    db.delete(conv)
    db.commit()
    return True


def list_messages(
    db: Session,
    conversation_id: int,
    limit: int = 200,
) -> list[dict]:
    # 展示过滤必须在 LIMIT 之前完成：工具轮密集的会话里，先取 200 行再过滤
    # 会让窗口内的可见消息越过滤越少，早期用户消息永久不可见。
    rows = (
        db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .where(Message.role.notin_(("tool", "system")))
            .where(
                func.coalesce(func.ltrim(Message.content), "").notlike(
                    '{"__tool_calls__"%'
                )
            )
            .order_by(Message.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": msg.id,
            "conversation_id": msg.conversation_id,
            "role": msg.role,
            "content": msg.content,
            "tool_trace": _json_loads(msg.tool_trace),
            "sources": _json_loads(msg.sources),
            "created_at": msg.created_at,
        }
        for msg in reversed(rows)
    ]


def get_message(db: Session, conversation_id: int, message_id: int) -> dict | None:
    """按 (会话, 消息 id) 直接取一条消息（rewind 用，不再拉时间窗口）。"""
    msg = db.get(Message, message_id)
    if msg is None or msg.conversation_id != conversation_id:
        return None
    return {
        "id": msg.id,
        "role": msg.role,
        "content": msg.content,
    }


def add_message(
    db: Session,
    conversation_id: int,
    role: str,
    content: str,
    tool_trace: list | None = None,
    sources: list | None = None,
) -> Message:
    msg = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        tool_trace=_json_dumps(truncate_tool_trace(tool_trace)),
        sources=_json_dumps(sanitize_sources(sources)),
    )
    db.add(msg)
    # 顺手刷新会话排序
    conv = db.get(Conversation, conversation_id)
    if conv is not None:
        conv.updated_at = datetime.now()
    db.commit()
    db.refresh(msg)
    return msg


def load_history_messages(
    db: Session,
    conversation_id: int,
    limit: int = 20,
) -> list[dict]:
    """加载最近 N 条消息（按时间正序），供 Agent 作为上下文。"""
    rows = (
        db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        {"role": msg.role, "content": msg.content}
        for msg in reversed(rows)
    ]


def list_messages_with_id(
    db: Session,
    conversation_id: int,
    limit: int = 500,
) -> list[dict]:
    """按时间正序返回消息（含 id），用于滚动摘要等场景。"""
    rows = (
        db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": msg.id,
            "role": msg.role,
            "content": msg.content,
            "tool_trace": msg.tool_trace,
        }
        for msg in rows
    ]


def get_messages_by_ids(db: Session, message_ids: list[int]) -> list[dict]:
    """按 id 批量取消息（时间线/回滚重建历史前缀用），保持 id 升序。"""
    if not message_ids:
        return []
    rows = (
        db.execute(
            select(Message)
            .where(Message.id.in_(message_ids))
            .order_by(Message.id.asc())
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": msg.id,
            "role": msg.role,
            "content": msg.content,
            "tool_trace": msg.tool_trace,
        }
        for msg in rows
    ]


def get_summary_state(db: Session, conversation_id: int) -> tuple[str | None, int]:
    """返回 (会话滚动摘要, 已纳入摘要的最大消息 id)。"""
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        return None, 0
    return conv.summary, conv.summary_up_to_id or 0


def save_summary(
    db: Session,
    conversation_id: int,
    summary: str,
    up_to_id: int,
) -> None:
    """保存滚动摘要并记录进度。"""
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        return
    conv.summary = summary
    conv.summary_up_to_id = up_to_id
    db.commit()


def clear_messages(db: Session, conversation_id: int) -> None:
    """清空某个会话的全部消息（用于 LangChain 记忆的 clear()）。"""
    db.execute(
        delete(Message).where(Message.conversation_id == conversation_id)
    )
    db.commit()


def list_message_rows_up_to(
    db: Session,
    conversation_id: int,
    message_id: int,
    limit: int = 5000,
) -> list[Message]:
    """会话中 id <= message_id 的全部消息行（含 tool/system 行，按 id 升序）。

    fork 专用：必须包含工具轮（assistant tool_calls 行与 tool 行），否则新
    会话从 MySQL 重建历史时缺行，与源会话实际发送的消息链不再逐字节一致。
    """
    return list(
        db.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.id <= message_id,
            )
            .order_by(Message.id.asc())
            .limit(limit)
        ).scalars()
    )


def fork_conversation(
    db: Session,
    conversation_id: int,
    message_id: int,
    title_suffix: str = "（分支）",
) -> dict | None:
    """从指定消息（含）之前复制出一个新会话。

    保留模板/工作区/提示词锁（提示词字节一致才可能继续命中 provider 前缀
    缓存）；滚动摘要因消息 id 全部变化而失效，重置为空。不复制原生检查点：
    检查点线程与 conv id 绑定，新会话下一轮由 MySQL 重建历史。
    """
    source = db.get(Conversation, conversation_id)
    if source is None:
        return None
    target = db.get(Message, message_id)
    if target is None or target.conversation_id != conversation_id:
        return None
    rows = list_message_rows_up_to(db, conversation_id, message_id)
    if not rows:
        return None
    suffix = title_suffix or ""
    base = (source.title or "新对话").strip() or "新对话"
    title = (base[: max(0, 200 - len(suffix))] + suffix)[:200]
    conv = Conversation(
        title=title,
        system_prompt=source.system_prompt,
        template_id=source.template_id,
        project_dir=source.project_dir,
        prompt_bundle=source.prompt_bundle,
        prompt_bundle_hash=source.prompt_bundle_hash,
        prompt_locked_at=source.prompt_locked_at,
        # 只记直接来源（fork 的 fork 指向上一个分支，不追溯根）
        forked_from_conversation_id=source.id,
        forked_from_message_id=message_id,
    )
    db.add(conv)
    db.flush()
    # 原样复制 JSON 列（不再 sanitize/truncate）：源行入库时已处理过，
    # 二次序列化会改变字节，破坏"与源会话前缀一致"的前提。
    db.add_all(
        [
            Message(
                conversation_id=conv.id,
                role=row.role,
                content=row.content,
                tool_trace=row.tool_trace,
                sources=row.sources,
            )
            for row in rows
        ]
    )
    db.commit()
    db.refresh(conv)
    return {"conversation": conv, "message_count": len(rows)}


def delete_messages_from(
    db: Session,
    conversation_id: int,
    message_id: int,
    inclusive: bool = True,
) -> int:
    """删除会话中 id >=（inclusive=True）或 > 该 id 的全部消息（消息级回退）。

    返回删除条数；顺带刷新会话 updated_at 保持列表排序正确。
    """
    cond = Message.id >= message_id if inclusive else Message.id > message_id
    result = db.execute(
        delete(Message).where(Message.conversation_id == conversation_id, cond)
    )
    conv = db.get(Conversation, conversation_id)
    if conv is not None:
        conv.updated_at = datetime.now()
    db.commit()
    return int(result.rowcount or 0)


def reset_summary_if_stale(
    db: Session, conversation_id: int, boundary_id: int
) -> bool:
    """回退边界早于摘要进度时重置滚动摘要（防止摘要残留已删除消息的内容）。

    摘要覆盖到 summary_up_to_id，若该进度超过回退边界，说明摘要里
    含有被删除消息的内容——直接清空，下次压缩时重新生成。
    """
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        return False
    if (conv.summary_up_to_id or 0) >= boundary_id and conv.summary:
        conv.summary = None
        conv.summary_up_to_id = 0
        db.commit()
        return True
    return False
