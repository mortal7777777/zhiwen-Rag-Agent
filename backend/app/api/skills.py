"""技能目录接口：列出 / 检索 / 启停 / 隐藏本机 Agent Skills。"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..rag.service import RAGService
from ..skills import (
    load_prefs,
    reset_prefs,
    save_prefs,
    scan_skills,
    search_skills,
)
from .deps import get_service

router = APIRouter(tags=["skills"])


class SkillPrefsPayload(BaseModel):
    """技能启停/隐藏偏好（按技能 id 批量设置）。"""

    enabled: list[str] = Field(default_factory=list)
    hidden: list[str] = Field(default_factory=list)


@router.get("/skills")
def list_skills(
    force: bool = False,
    include_hidden: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict:
    """列出全部可复用技能（Codex / Claude / Hermes），默认不含已隐藏项。"""
    load_prefs(db, force=True)
    skills = scan_skills(force=force)
    if not include_hidden:
        skills = [s for s in skills if not s.get("hidden")]
    prefs = load_prefs(db)
    return {
        "total": len(skills),
        "items": skills,
        "prefs": {
            "enabled": sorted(prefs["enabled"]),
            "hidden": sorted(prefs["hidden"]),
            "custom": prefs["custom"],
        },
    }


@router.get("/skills/search")
def skills_search(
    q: str = Query(..., min_length=1, max_length=200),
    top_k: int = Query(default=5, ge=1, le=20),
    include_hidden: bool = Query(default=False),
    service: RAGService = Depends(get_service),
    db: Session = Depends(get_db),
) -> dict:
    """按语义/关键词检索技能（设置页浏览用，返回全部未隐藏技能）。

    Agent 的 skill_lookup 只检索已启用技能，由 tools.py 传入 enabled_ids 过滤。
    """
    hits = search_skills(
        q,
        embeddings=service.embeddings,
        top_k=top_k,
    )
    if not include_hidden:
        hits = [s for s in hits if not s.get("hidden")]
    return {"total": len(hits), "items": hits}


@router.put("/skills")
def update_skills(
    payload: SkillPrefsPayload = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    """保存技能偏好：enabled=本助手启用的技能，hidden=从本助手移除的技能。

    只影响本助手的 skill_lookup，绝不修改其他 Agent 的技能文件。
    """
    prefs = save_prefs(db, enabled=payload.enabled, hidden=payload.hidden)
    return {
        "ok": True,
        "prefs": {
            "enabled": sorted(prefs["enabled"]),
            "hidden": sorted(prefs["hidden"]),
            "custom": prefs["custom"],
        },
    }


@router.post("/skills/reset")
def reset_skill_prefs(db: Session = Depends(get_db)) -> dict:
    """恢复默认精选手集。"""
    prefs = reset_prefs(db)
    return {
        "ok": True,
        "prefs": {
            "enabled": sorted(prefs["enabled"]),
            "hidden": sorted(prefs["hidden"]),
            "custom": prefs["custom"],
        },
    }
