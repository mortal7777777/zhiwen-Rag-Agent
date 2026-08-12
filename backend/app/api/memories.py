"""长期事实记忆接口：列表 / 新增 / 编辑 / 删除 / 手动整合整理。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..agent.context import MEMORY_CATEGORIES
from ..db import get_db
from ..db import repository as repo
from .deps import get_agent_service

router = APIRouter(tags=["memories"])


class MemoryOut(BaseModel):
    id: int
    content: str
    category: str
    status: str
    source_conversation_id: int | None = None
    created_at: object
    updated_at: object


class MemoryCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)
    category: str = Field(default="other", max_length=50)


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    category: str | None = Field(default=None, max_length=50)


def _valid_category(category: str) -> bool:
    return category in MEMORY_CATEGORIES


@router.get("/memories", response_model=list[MemoryOut])
def list_memories(db: Session = Depends(get_db)) -> list[dict]:
    """查看活跃的长期记忆（按更新时间倒序）。"""
    return repo.list_memories(db, limit=500)


@router.post("/memories", response_model=MemoryOut)
def create_memory(
    payload: MemoryCreate,
    db: Session = Depends(get_db),
) -> dict:
    """手动新增一条长期记忆。"""
    category = payload.category if _valid_category(payload.category) else "other"
    m = repo.add_memory(db, payload.content, category=category)
    return {
        "id": m.id,
        "content": m.content,
        "category": m.category,
        "status": m.status,
        "source_conversation_id": m.source_conversation_id,
        "created_at": m.created_at,
        "updated_at": m.updated_at,
    }


@router.patch("/memories/{memory_id}", response_model=MemoryOut)
def update_memory(
    memory_id: int,
    payload: MemoryUpdate,
    db: Session = Depends(get_db),
) -> dict:
    """编辑记忆内容或分类。"""
    category = payload.category
    if category is not None and not _valid_category(category):
        raise HTTPException(status_code=400, detail=f"分类不合法，可选：{'/'.join(MEMORY_CATEGORIES)}")
    m = repo.update_memory(
        db,
        memory_id,
        content=payload.content,
        category=category,
    )
    if m is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {
        "id": m.id,
        "content": m.content,
        "category": m.category,
        "status": m.status,
        "source_conversation_id": m.source_conversation_id,
        "created_at": m.created_at,
        "updated_at": m.updated_at,
    }


@router.delete("/memories/{memory_id}", status_code=204)
def delete_memory(memory_id: int, db: Session = Depends(get_db)) -> None:
    """删除一条记忆。"""
    if not repo.delete_memory(db, memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在")


@router.post("/memories/consolidate")
def consolidate_memories(
    db: Session = Depends(get_db),
    agent_service=Depends(get_agent_service),
) -> dict:
    """手动触发一次记忆整合整理：合并重复、覆盖过时、生成用户画像摘要。"""
    return agent_service.context.consolidate_memories(db)
