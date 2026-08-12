"""提示词模板接口：列表、新建、编辑、删除（内置模板只读）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..db import repository as repo
from ..schemas import TemplateCreate, TemplateOut, TemplateUpdate

router = APIRouter(tags=["templates"])


@router.get("/templates", response_model=list[TemplateOut])
def list_templates(db: Session = Depends(get_db)) -> list[dict]:
    """全部提示词模板（内置在前，自定义在后）。"""
    return repo.list_templates(db)


@router.post("/templates", response_model=TemplateOut)
def create_template(
    payload: TemplateCreate,
    db: Session = Depends(get_db),
) -> dict:
    """新建自定义提示词模板。"""
    tpl = repo.create_template(
        db,
        name=payload.name,
        content=payload.content,
        description=payload.description,
        category=payload.category,
    )
    return {
        "id": tpl.id,
        "name": tpl.name,
        "description": tpl.description,
        "category": tpl.category,
        "content": tpl.content,
        "is_system": False,
        "created_at": tpl.created_at,
        "updated_at": tpl.updated_at,
    }


@router.patch("/templates/{template_id}", response_model=TemplateOut)
def update_template(
    template_id: int,
    payload: TemplateUpdate,
    db: Session = Depends(get_db),
) -> dict:
    """编辑模板（内置模板只读）。"""
    tpl = repo.get_template(db, template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    if tpl.is_system:
        raise HTTPException(status_code=400, detail="内置模板不可编辑，请新建自定义模板")
    tpl = repo.update_template(
        db,
        template_id,
        name=payload.name,
        content=payload.content,
        description=payload.description,
        category=payload.category,
    )
    return {
        "id": tpl.id,
        "name": tpl.name,
        "description": tpl.description,
        "category": tpl.category,
        "content": tpl.content,
        "is_system": False,
        "created_at": tpl.created_at,
        "updated_at": tpl.updated_at,
    }


@router.delete("/templates/{template_id}", status_code=204)
def delete_template(template_id: int, db: Session = Depends(get_db)) -> None:
    """删除自定义模板（内置模板不可删除）。"""
    if not repo.delete_template(db, template_id):
        raise HTTPException(status_code=404, detail="模板不存在或为内置模板")
