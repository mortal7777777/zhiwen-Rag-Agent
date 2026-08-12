"""会话接口：列表、新建、重命名、删除、消息历史。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..db import repository as repo
from ..schemas import (
    ConversationCreate,
    ConversationOut,
    ConversationRename,
    MessageOut,
)

router = APIRouter(tags=["conversations"])


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(db: Session = Depends(get_db)) -> list[dict]:
    """最近更新的会话列表（附带消息条数）。"""
    return repo.list_conversations(db)


@router.post("/conversations", response_model=ConversationOut)
def create_conversation(
    payload: ConversationCreate | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """新建一个空会话。"""
    title = (payload.title if payload else None) or "新对话"
    template_id = (payload.template_id if payload else None) or None
    conv = repo.create_conversation(db, title=title, template_id=template_id)
    return {
        "id": conv.id,
        "title": conv.title,
        "template_id": conv.template_id,
        "system_prompt": conv.system_prompt,
        "created_at": conv.created_at,
        "updated_at": conv.updated_at,
        "message_count": 0,
    }


@router.patch("/conversations/{conversation_id}", response_model=ConversationOut)
def rename_conversation(
    conversation_id: int,
    payload: ConversationRename,
    db: Session = Depends(get_db),
) -> dict:
    """更新会话：重命名 / 绑定提示词模板。"""
    template_id = payload.template_id  # 0 = 切回默认模板，None = 不修改
    conv = repo.update_conversation(
        db,
        conversation_id,
        title=payload.title,
        template_id=template_id,
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {
        "id": conv.id,
        "title": conv.title,
        "template_id": conv.template_id,
        "system_prompt": conv.system_prompt,
        "created_at": conv.created_at,
        "updated_at": conv.updated_at,
        "message_count": 0,
    }


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: int, db: Session = Depends(get_db)) -> None:
    """删除会话及其全部消息。"""
    if not repo.delete_conversation(db, conversation_id):
        raise HTTPException(status_code=404, detail="会话不存在")


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
)
def list_messages(conversation_id: int, db: Session = Depends(get_db)) -> list[dict]:
    """加载某个会话的全部历史消息。"""
    if repo.get_conversation(db, conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return repo.list_messages(db, conversation_id)
