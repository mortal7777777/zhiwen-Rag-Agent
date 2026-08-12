"""数据访问层：把 SQLAlchemy 查询封装成业务函数，API 层不直接写 SQL。"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import AgentRun, AppMeta, Conversation, Memory, Message, PromptTemplate

logger = logging.getLogger(__name__)


def _json_dumps(obj) -> str | None:
    return json.dumps(obj, ensure_ascii=False) if obj is not None else None


def _json_loads(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


# ---------------- 会话 ----------------

def create_conversation(
    db: Session,
    title: str = "新对话",
    system_prompt: str | None = None,
    template_id: int | None = None,
) -> Conversation:
    conv = Conversation(
        title=title,
        system_prompt=system_prompt,
        template_id=template_id,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def list_conversations(db: Session, limit: int = 200) -> list[dict]:
    """按最近更新排序，附带消息条数。"""
    rows = db.execute(
        select(
            Conversation,
            func.count(Message.id).label("message_count"),
        )
        .outerjoin(Message, Message.conversation_id == Conversation.id)
        .group_by(Conversation.id)
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": conv.id,
            "title": conv.title,
            "template_id": conv.template_id,
            "system_prompt": conv.system_prompt,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
            "message_count": count,
        }
        for conv, count in rows
    ]


def get_conversation(db: Session, conversation_id: int) -> Conversation | None:
    return db.get(Conversation, conversation_id)


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
) -> Conversation | None:
    """更新会话字段（标题 / 绑定的模板）。template_id=None 表示清空绑定。"""
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        return None
    if title is not None:
        conv.title = title.strip()[:200] or "新对话"
    if template_id is not None:
        # 0 = 切回默认模板（清空绑定）；None = 不修改
        conv.template_id = template_id or None
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


# ---------------- 消息 ----------------

def list_messages(
    db: Session,
    conversation_id: int,
    limit: int = 200,
) -> list[dict]:
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
    messages = [
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
    return messages


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
        tool_trace=_json_dumps(tool_trace),
        sources=_json_dumps(sources),
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
        {"id": msg.id, "role": msg.role, "content": msg.content}
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


# ---------------- Agent 运行记录（决策可观测性）----------------

def create_agent_run(
    db: Session,
    *,
    conversation_id: int | None,
    question: str,
    plan: list | None,
    tool_trace: list | None,
    answer_len: int,
    latency_ms: int,
    status: str,
    error: str | None,
    token_usage: dict | None,
) -> AgentRun:
    run = AgentRun(
        conversation_id=conversation_id,
        question=question[:4000],
        plan=_json_dumps(plan),
        tool_trace=_json_dumps(tool_trace),
        answer_len=answer_len,
        latency_ms=latency_ms,
        status=status,
        error=(error or "")[:4000] or None,
        token_usage=_json_dumps(token_usage),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def list_agent_runs(
    db: Session,
    limit: int = 50,
    conversation_id: int | None = None,
) -> list[dict]:
    query = select(AgentRun).order_by(AgentRun.id.desc()).limit(limit)
    if conversation_id is not None:
        query = (
            select(AgentRun)
            .where(AgentRun.conversation_id == conversation_id)
            .order_by(AgentRun.id.desc())
            .limit(limit)
        )
    rows = db.execute(query).scalars().all()
    return [
        {
            "id": r.id,
            "conversation_id": r.conversation_id,
            "question": r.question,
            "plan": _json_loads(r.plan),
            "tool_trace": _json_loads(r.tool_trace),
            "answer_len": r.answer_len,
            "latency_ms": r.latency_ms,
            "status": r.status,
            "error": r.error,
            "token_usage": _json_loads(r.token_usage),
            "created_at": r.created_at,
        }
        for r in rows
    ]


# ---------------- 提示词模板 ----------------

def list_templates(db: Session) -> list[dict]:
    rows = db.execute(
        select(PromptTemplate).order_by(PromptTemplate.is_system.desc(), PromptTemplate.id)
    ).scalars().all()
    return [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "category": t.category,
            "content": t.content,
            "is_system": t.is_system,
            "created_at": t.created_at,
            "updated_at": t.updated_at,
        }
        for t in rows
    ]


def get_template(db: Session, template_id: int) -> PromptTemplate | None:
    return db.get(PromptTemplate, template_id)


def create_template(
    db: Session,
    name: str,
    content: str,
    description: str = "",
    category: str = "general",
) -> PromptTemplate:
    tpl = PromptTemplate(
        name=name.strip()[:100],
        content=content,
        description=description.strip()[:255],
        category=category.strip()[:50] or "general",
        is_system=False,
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


def update_template(
    db: Session,
    template_id: int,
    name: str | None = None,
    content: str | None = None,
    description: str | None = None,
    category: str | None = None,
) -> PromptTemplate | None:
    tpl = db.get(PromptTemplate, template_id)
    if tpl is None:
        return None
    if name is not None:
        tpl.name = name.strip()[:100]
    if content is not None:
        tpl.content = content
    if description is not None:
        tpl.description = description.strip()[:255]
    if category is not None:
        tpl.category = category.strip()[:50] or "general"
    db.commit()
    db.refresh(tpl)
    return tpl


def delete_template(db: Session, template_id: int) -> bool:
    tpl = db.get(PromptTemplate, template_id)
    if tpl is None or tpl.is_system:
        return False
    db.delete(tpl)
    db.commit()
    return True


def seed_templates(db: Session) -> None:
    """幂等写入/同步内置提示词模板。

    已存在的系统模板（is_system=True）会在启动时按 PRESET_TEMPLATES
    更新内容/描述/分类，保证代码里的提示词优化能生效；
    用户自建模板不受影响。
    """
    from ..agent.prompts import PRESET_TEMPLATES

    for preset in PRESET_TEMPLATES:
        row = (
            db.execute(
                select(PromptTemplate).where(
                    PromptTemplate.name == preset["name"]
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            db.add(
                PromptTemplate(
                    name=preset["name"],
                    description=preset["description"],
                    category=preset["category"],
                    content=preset["content"],
                    is_system=True,
                )
            )
            continue
        if row.is_system:
            row.description = preset["description"]
            row.category = preset["category"]
            row.content = preset["content"]
    db.commit()


# ---------------- 长期事实记忆 ----------------

def list_memories(
    db: Session,
    limit: int = 500,
    category: str | None = None,
    status: str = "active",
) -> list[dict]:
    rows = (
        db.execute(
            select(Memory)
            .where(Memory.status == status)
            .order_by(Memory.updated_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    if category:
        rows = [m for m in rows if m.category == category]
    return [
        {
            "id": m.id,
            "content": m.content,
            "category": m.category,
            "status": m.status,
            "source_conversation_id": m.source_conversation_id,
            "created_at": m.created_at,
            "updated_at": m.updated_at,
        }
        for m in rows
    ]


def add_memory(
    db: Session,
    content: str,
    category: str = "other",
    source_conversation_id: int | None = None,
    status: str = "active",
) -> Memory:
    m = Memory(
        content=content,
        category=category,
        source_conversation_id=source_conversation_id,
        status=status,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def update_memory(
    db: Session,
    memory_id: int,
    content: str | None = None,
    category: str | None = None,
    status: str | None = None,
) -> Memory | None:
    """编辑记忆内容/分类/状态。"""
    m = db.get(Memory, memory_id)
    if m is None:
        return None
    if content is not None:
        m.content = content
    if category is not None:
        m.category = category
    if status is not None:
        m.status = status
    db.commit()
    db.refresh(m)
    return m


def delete_memory(db: Session, memory_id: int) -> bool:
    m = db.get(Memory, memory_id)
    if m is None:
        return False
    db.delete(m)
    db.commit()
    return True


def list_all_memories(db: Session, limit: int = 2000) -> list[dict]:
    """全部记忆（含已归档），供整合整理使用。"""
    rows = (
        db.execute(select(Memory).order_by(Memory.updated_at.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [
        {
            "id": m.id,
            "content": m.content,
            "category": m.category,
            "status": m.status,
            "source_conversation_id": m.source_conversation_id,
            "created_at": m.created_at,
            "updated_at": m.updated_at,
        }
        for m in rows
    ]


def set_meta(db: Session, key: str, value: str) -> None:
    row = db.get(AppMeta, key)
    if row is None:
        db.add(AppMeta(key=key, value=value))
    else:
        row.value = value
    db.commit()


def get_meta(db: Session, key: str) -> str | None:
    row = db.get(AppMeta, key)
    return row.value if row is not None else None
