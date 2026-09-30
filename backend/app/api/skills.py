"""技能目录接口：列出 / 检索 / 启停 / 隐藏本机 Agent Skills。"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..rag.service import RAGService
from ..skills import (
    agent_skill_content,
    delete_agent_skill,
    load_prefs,
    reset_prefs,
    save_prefs,
    scan_skills,
    search_skills,
    share_agent_skill,
    unshare_agent_skill,
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


class AgentSkillPayload(BaseModel):
    """自创技能操作载荷：dir_name 为技能目录名，category 为分类目录。"""

    dir_name: str = Field(min_length=1, max_length=80)
    category: str = Field(default="", max_length=80)


def _agent_skill_or_http(action) -> dict:
    """统一错误映射：未找到 → 404，拒绝删除 → 403。"""
    try:
        return action()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/skills/agent/share")
def share_agent_skill_api(payload: AgentSkillPayload = Body(...)) -> dict:
    """把自创技能共享给 Hermes（复制到 Hermes 个人技能层）。"""
    return _agent_skill_or_http(lambda: share_agent_skill(payload.dir_name, payload.category))


@router.post("/skills/agent/unshare")
def unshare_agent_skill_api(payload: AgentSkillPayload = Body(...)) -> dict:
    """取消共享（删除 Hermes 个人层里的副本，自创技能库保留原技能）。"""
    return _agent_skill_or_http(
        lambda: unshare_agent_skill(payload.dir_name, payload.category)
    )


@router.delete("/skills/agent/{dir_name}")
def delete_agent_skill_api(
    dir_name: str,
    category: str = Query(default="", max_length=80),
) -> dict:
    """删除自创技能（已共享的副本一并清理，仅限 origin=agent 的目录）。"""
    return _agent_skill_or_http(lambda: delete_agent_skill(dir_name, category))


@router.get("/skills/agent/content")
def agent_skill_content_api(
    dir_name: str = Query(..., min_length=1, max_length=80),
    category: str = Query(default="", max_length=80),
) -> dict:
    """读取自创技能 SKILL.md 全文（预览）。"""
    return _agent_skill_or_http(lambda: agent_skill_content(dir_name, category))
