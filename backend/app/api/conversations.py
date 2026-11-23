"""会话接口：列表、新建、重命名、删除、消息历史、消息级回退。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..db import repository as repo
from ..schemas import (
    ConversationCreate,
    ConversationForkRequest,
    ConversationOut,
    ConversationSearchOut,
    ConversationUpdate,
    MessageOut,
    RewindRequest,
)
from ..todos import save_todos
from .agent import is_conversation_active
from .deps import get_agent_service

router = APIRouter(tags=["conversations"])


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(db: Session = Depends(get_db)) -> list[dict]:
    """最近更新的会话列表（附带消息条数）。"""
    return repo.list_conversations(db)


@router.get("/conversations/search", response_model=list[ConversationSearchOut])
def search_conversations_endpoint(
    q: str = Query(..., min_length=1, max_length=100, description="搜索词"),
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
) -> list[dict]:
    """按标题、工作区与消息正文搜索会话（最近更新优先，带命中数与片段）。"""
    return repo.search_conversations(db, q, limit=limit)


@router.post("/conversations", response_model=ConversationOut)
def create_conversation(
    payload: ConversationCreate | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """新建一个空会话。"""
    title = (payload.title if payload else None) or "新对话"
    template_id = (payload.template_id if payload else None) or None
    project_dir = (payload.project_dir if payload else None) or None
    conv = repo.create_conversation(
        db, title=title, template_id=template_id, project_dir=project_dir
    )
    return {
        "id": conv.id,
        "title": conv.title,
        "template_id": conv.template_id,
        "project_dir": conv.project_dir,
        "system_prompt": conv.system_prompt,
        "prompt_bundle_hash": conv.prompt_bundle_hash,
        "prompt_locked_at": conv.prompt_locked_at,
        "forked_from_conversation_id": conv.forked_from_conversation_id,
        "forked_from_message_id": conv.forked_from_message_id,
        "pinned": False,
        "created_at": conv.created_at,
        "updated_at": conv.updated_at,
        "message_count": 0,
    }


@router.patch("/conversations/{conversation_id}", response_model=ConversationOut)
def update_conversation_endpoint(
    conversation_id: int,
    payload: ConversationUpdate,
    db: Session = Depends(get_db),
    agent=Depends(get_agent_service),
) -> dict:
    """更新会话：重命名 / 绑定提示词模板 / 置顶（只改传了的字段）。"""
    template_id = payload.template_id  # 0 = 切回默认模板，None = 不修改
    if template_id is not None:
        existing = repo.get_conversation(db, conversation_id)
        if existing is not None and (existing.template_id or None) != (
            template_id or None
        ):
            if template_id:
                target = repo.get_template(db, template_id)
                if target is not None and target.archived:
                    raise HTTPException(
                        status_code=409, detail="该模板已归档，不能切换过去"
                    )
            if is_conversation_active(conversation_id):
                raise HTTPException(
                    status_code=409, detail="会话正在运行，先停止后再切换模板"
                )
            try:
                pending = (
                    agent.pending_state(conversation_id)
                    if agent is not None
                    else {}
                )
            except Exception:
                pending = {}
            if pending.get("pending") or pending.get("interrupted"):
                raise HTTPException(
                    status_code=409,
                    detail="会话有挂起或未完成任务，先处理后再切换模板",
                )
    conv = repo.update_conversation(
        db,
        conversation_id,
        title=payload.title,
        template_id=template_id,
        project_dir=payload.project_dir,
        pinned=payload.pinned,
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {
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
        "message_count": 0,
    }


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    agent=Depends(get_agent_service),
) -> None:
    """删除会话及其全部消息 + 原生检查点线程（否则快照会在 sqlite 里越积越多）。"""
    if is_conversation_active(conversation_id):
        raise HTTPException(status_code=409, detail="会话正在运行，先停止后再删除")
    if not repo.delete_conversation(db, conversation_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    abandon = getattr(agent, "abandon_run", None)
    if callable(abandon):
        try:
            abandon(conversation_id)
        except Exception:
            pass


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
)
def list_messages(conversation_id: int, db: Session = Depends(get_db)) -> list[dict]:
    """加载某个会话的全部历史消息。"""
    if repo.get_conversation(db, conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return repo.list_messages(db, conversation_id)


@router.post(
    "/conversations/{conversation_id}/fork",
    response_model=ConversationOut,
)
def fork_conversation_endpoint(
    conversation_id: int,
    payload: ConversationForkRequest,
    db: Session = Depends(get_db),
) -> dict:
    """会话级分支：复制目标消息（含）之前的消息到新会话（6.9）。

    保留模板/工作区/提示词锁，重置滚动摘要；不复制原生检查点。运行中的
    会话拒绝分支（本轮消息尚未落库，复制出来的前缀不完整）。
    """
    conv = repo.get_conversation(db, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if repo.get_message(db, conversation_id, payload.message_id) is None:
        raise HTTPException(status_code=404, detail="消息不存在或不属于该会话")
    if is_conversation_active(conversation_id):
        raise HTTPException(status_code=409, detail="会话正在运行，先停止后再分支")
    forked = repo.fork_conversation(db, conversation_id, payload.message_id)
    if forked is None:
        raise HTTPException(status_code=404, detail="消息不存在或不属于该会话")
    new_conv = forked["conversation"]
    return {
        "id": new_conv.id,
        "title": new_conv.title,
        "template_id": new_conv.template_id,
        "project_dir": new_conv.project_dir,
        "system_prompt": new_conv.system_prompt,
        "prompt_bundle_hash": new_conv.prompt_bundle_hash,
        "prompt_locked_at": new_conv.prompt_locked_at,
        "forked_from_conversation_id": new_conv.forked_from_conversation_id,
        "forked_from_message_id": new_conv.forked_from_message_id,
        "pinned": bool(new_conv.pinned),
        "created_at": new_conv.created_at,
        "updated_at": new_conv.updated_at,
        "message_count": int(forked["message_count"]),
    }


@router.post("/conversations/{conversation_id}/rewind")
def rewind_conversation(
    conversation_id: int,
    payload: RewindRequest,
    db: Session = Depends(get_db),
    agent=Depends(get_agent_service),
) -> dict:
    """消息级回退：删除该消息及其之后的全部消息（类 Claude Code rewind）。

    同时清理附属状态，保证回退干净：
    - 滚动摘要若覆盖了被删消息 → 重置（防摘要残留旧内容）；
    - 该会话的任务清单（todos）清空；
    - 原生检查点线程删除（防止恢复到已回退的状态）。
    """
    conv = repo.get_conversation(db, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if is_conversation_active(conversation_id):
        raise HTTPException(status_code=409, detail="会话正在运行，先停止后再回退")
    target = repo.get_message(db, conversation_id, payload.message_id)
    if target is None:
        raise HTTPException(status_code=404, detail="消息不存在或不属于该会话")
    removed = repo.delete_messages_from(db, conversation_id, payload.message_id)
    summary_reset = repo.reset_summary_if_stale(db, conversation_id, payload.message_id)
    try:
        save_todos(db, conversation_id, [])
    except Exception:
        pass
    abandon = getattr(agent, "abandon_run", None)
    if callable(abandon):
        try:
            abandon(conversation_id)
        except Exception:
            pass
    return {
        "conversation_id": conversation_id,
        "message_id": payload.message_id,
        "removed": removed,
        "summary_reset": summary_reset,
        "rewound_content": target.get("content") or "",
    }
