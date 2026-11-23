"""长期记忆与应用元数据访问。"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..models import AppMeta, Memory
from ..sanitize import sanitize_sources, sanitize_token_usage, truncate_tool_trace

logger = logging.getLogger(__name__)


def list_memories(
    db: Session,
    limit: int = 500,
    category: str | None = None,
    status: str = "active",
    order: str = "updated",
) -> list[dict]:
    """按状态列出记忆。

    order="updated"（默认）：内容最近修改在前——记忆面板、AGENTS.md 导出用；
    order="created"：录入时间最新在前——召回兜底要的是"最近的事实"，而不是
      "最近被编辑过的"（updated_at 只反映内容编辑，与事实新旧无关）。

    两种排序都以 id 降序兜底：updated_at 可能大量并列（批量恢复、合并会把多行
    写成同一时间戳），没有 tiebreaker 时返回顺序不确定，"取前 N 条"会取到任意
    条目（实测 46 条里 44 条并列，兜底取到的是两条随机的旧记忆）。
    """
    key = Memory.created_at if order == "created" else Memory.updated_at
    rows = (
        db.execute(
            select(Memory)
            .where(Memory.status == status)
            .order_by(key.desc(), Memory.id.desc())
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
            "hit_count": m.hit_count or 0,
            "last_hit_at": m.last_hit_at,
            "created_at": m.created_at,
            "updated_at": m.updated_at,
        }
        for m in rows
    ]


def bump_memory_hits(db: Session, memory_ids: list[int]) -> None:
    """记忆被注入上下文时累加命中计数（仅供观测；不作整合归档依据，见 Memory 模型注释）。"""
    ids = [int(i) for i in (memory_ids or []) if i]
    if not ids:
        return
    now = datetime.now()
    for mid in ids:
        m = db.get(Memory, mid)
        if m is None:
            continue
        m.hit_count = int(m.hit_count or 0) + 1
        m.last_hit_at = now
    db.commit()


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
    """编辑记忆内容/分类/状态。

    updated_at 只由**内容/分类**变化刷新（模型列已去掉 onupdate）：
    - 命中计数累加（bump_memory_hits）不刷新——否则"最后被召回"会被当成
      "最后被陈述"，整合的新旧判定与导出排序双双失真；
    - 状态变更（归档/恢复）也不刷新——那是管理动作，不是内容编辑；否则归档一次
      就会把旧记忆顶到"最近"的位置（历史事故：批量恢复把 44 行写成同一时间戳，
      与召回分一起让排序彻底失去区分度）。
    """
    m = db.get(Memory, memory_id)
    if m is None:
        return None
    changed = False
    if content is not None and content != m.content:
        m.content = content
        changed = True
    if category is not None and category != m.category:
        m.category = category
        changed = True
    if status is not None and status != m.status:
        m.status = status
        # 状态不算内容编辑：不置 changed，不刷新 updated_at
    if changed:
        m.updated_at = datetime.now()
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
    """全部记忆（含已归档），供整合整理使用。

    排序带 id 降序兜底：整合会按 created_at 自行重排，但 limit 截断发生在排序
    之后，updated_at 并列时若不确定，截断掉的可能是任意的条目。
    """
    rows = (
        db.execute(
            select(Memory)
            .order_by(Memory.updated_at.desc(), Memory.id.desc())
            .limit(limit)
        )
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
            "hit_count": m.hit_count or 0,
            "last_hit_at": m.last_hit_at,
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
