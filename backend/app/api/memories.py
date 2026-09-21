"""长期事实记忆接口：列表 / 新增 / 编辑 / 删除 / 手动整合整理。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..agent.context import MEMORY_CATEGORIES
from ..db import get_db
from ..db import repository as repo
from .deps import get_agent_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["memories"])


def _sync_project_memory(db: Session) -> None:
    """记忆增删改后同步导出文件型项目记忆（AGENTS.md）。

    只靠"新增记忆 + 整合"路径导出时，手动删除/编辑记忆不会反映到
    AGENTS.md，会话注入的项目记忆会与记忆库长期不一致。
    """
    try:
        from ..config import get_settings
        from ..project_memory import export_project_memory

        export_project_memory(get_settings(), db)
    except Exception as exc:
        logger.warning("项目记忆导出失败：%s", exc)


class MemoryOut(BaseModel):
    id: int
    content: str
    category: str
    status: str
    source_conversation_id: int | None = None
    hit_count: int = 0          # 被注入上下文的累计次数（判断"有用"的依据）
    last_hit_at: object | None = None
    created_at: object
    updated_at: object


class MemoryCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)
    category: str = Field(default="other", max_length=50)


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    category: str | None = Field(default=None, max_length=50)
    # 归档 = 停用但保留：不再参与召回/导出，可随时恢复（对比删除不可逆）
    status: str | None = Field(default=None, pattern="^(active|archived)$")


def _valid_category(category: str) -> bool:
    return category in MEMORY_CATEGORIES


@router.get("/memories", response_model=list[MemoryOut])
def list_memories(
    status: str = "active",
    db: Session = Depends(get_db),
) -> list[dict]:
    """查看长期记忆（默认只看活跃；status=archived 看归档，all 看全部）。"""
    if status == "all":
        return repo.list_all_memories(db, limit=500)
    if status == "archived":
        return [
            m
            for m in repo.list_all_memories(db, limit=500)
            if (m.get("status") or "") == "archived"
        ]
    return repo.list_memories(db, limit=500)


@router.post("/memories", response_model=MemoryOut)
def create_memory(
    payload: MemoryCreate,
    db: Session = Depends(get_db),
) -> dict:
    """手动新增一条长期记忆。"""
    category = payload.category if _valid_category(payload.category) else "other"
    m = repo.add_memory(db, payload.content, category=category)
    _sync_project_memory(db)
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
        status=payload.status,
    )
    if m is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    _sync_project_memory(db)
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
    _sync_project_memory(db)


@router.post("/memories/consolidate")
def consolidate_memories(
    db: Session = Depends(get_db),
    agent_service=Depends(get_agent_service),
) -> dict:
    """手动触发一次记忆整合整理：合并重复、覆盖过时、生成用户画像摘要。"""
    result = agent_service.context.consolidate_memories(db)
    _sync_project_memory(db)
    return result
