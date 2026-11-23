"""提示词管理工作台 API（模板 + 片段 + 试跑 + 审计）。

2026-11-13 合并自 `api/templates.py` 与 `api/prompts.py`：同一份数据原先有两套
入口，改模板语义要改两处。现按 fragments / templates / trials / audit 四个
子路由组织，URL 与响应结构保持原样。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.orm import Session

from ..agent.prompts import (
    FRAGMENTS_BY_KEY,
    compose_system_prompt,
    find_mid_sentence_breaks,
    lint_fragment_content,
    lint_template_content,
    normalize_mid_sentence_breaks,
    validate_fragment_content,
)
from ..db import get_db
from ..db import repository as repo
from ..llm_text import message_text
from ..prompting import (
    apply_latest,
    conversation_prompt_status,
    pin_conversation_prompt,
    preview_prompt_resolution,
)
from ..runtime_config import effective_or
from ..schemas import (
    PromptBatchApply,
    PromptDraftPublish,
    PromptDraftSave,
    PromptFragmentUpdate,
    PromptImportRequest,
    PromptLintRequest,
    PromptPinRequest,
    PromptPreviewRequest,
    PromptTrialABRequest,
    PromptTrialEvalRequest,
    PromptVersionRestore,
    TemplateCreate,
    TemplateOut,
    TemplateUpdate,
)
from ..todos import load_todos, todos_to_text
from .agent import is_conversation_active
from .deps import get_agent_service
from .prompt_auth import require_prompt_admin

_DEP = [Depends(require_prompt_admin)]
templates_router = APIRouter(tags=["templates"], dependencies=_DEP)
fragments_router = APIRouter(tags=["prompts"], dependencies=_DEP)
trials_router = APIRouter(tags=["prompts"], dependencies=_DEP)
audit_router = APIRouter(tags=["prompts"], dependencies=_DEP)


def _template_out(db: Session, tpl) -> dict:
    versions = repo.list_template_versions(db, tpl.id)
    latest = versions[0] if versions else None
    return {
        "id": tpl.id,
        "name": tpl.name,
        "description": tpl.description,
        "category": tpl.category,
        "content": tpl.content,
        "is_system": tpl.is_system,
        "system_key": tpl.system_key,
        "archived": bool(tpl.archived),
        "draft_content": tpl.draft_content,
        "draft_note": tpl.draft_note or "",
        "draft_updated_at": tpl.draft_updated_at,
        "has_draft": bool(tpl.draft_content),
        "content_hash": latest["content_hash"] if latest else None,
        "latest_version_id": latest["id"] if latest else None,
        "latest_version_no": latest["version_no"] if latest else None,
        "version_count": len(versions),
        "created_at": tpl.created_at,
        "updated_at": tpl.updated_at,
    }


@templates_router.get("/templates", response_model=list[TemplateOut])
def list_templates(db: Session = Depends(get_db)) -> list[dict]:
    """全部提示词模板（内置在前，自定义在后）。"""
    return repo.list_templates(db)


@templates_router.post("/templates", response_model=TemplateOut)
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
    return _template_out(db, tpl)


@templates_router.post("/templates/lint")
def lint_template(payload: PromptLintRequest) -> dict:
    """模板保存前检查：长度、未解析占位符、句中硬换行。"""
    warnings = lint_template_content(payload.content)
    return {"ok": True, "errors": [], "warnings": warnings}


@templates_router.patch("/templates/{template_id}", response_model=TemplateOut)
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
        version_note=payload.version_note or "",
    )
    return _template_out(db, tpl)


@templates_router.post("/templates/{template_id}/archive", response_model=TemplateOut)
def archive_template(template_id: int, db: Session = Depends(get_db)) -> dict:
    tpl = repo.archive_template(db, template_id, archived=True)
    if tpl is None:
        raise HTTPException(status_code=404, detail="模板不存在或为内置模板")
    return _template_out(db, tpl)


@templates_router.post("/templates/{template_id}/unarchive", response_model=TemplateOut)
def unarchive_template(template_id: int, db: Session = Depends(get_db)) -> dict:
    tpl = repo.archive_template(db, template_id, archived=False)
    if tpl is None:
        raise HTTPException(status_code=404, detail="模板不存在或为内置模板")
    return _template_out(db, tpl)


@templates_router.put("/templates/{template_id}/draft", response_model=TemplateOut)
def save_template_draft(
    template_id: int,
    payload: PromptDraftSave,
    db: Session = Depends(get_db),
) -> dict:
    tpl = repo.save_template_draft(
        db, template_id, payload.content, payload.note
    )
    if tpl is None:
        raise HTTPException(status_code=404, detail="模板不存在或为内置模板")
    return _template_out(db, tpl)


@templates_router.post("/templates/{template_id}/publish-draft", response_model=TemplateOut)
def publish_template_draft(
    template_id: int,
    payload: PromptDraftPublish | None = None,
    db: Session = Depends(get_db),
) -> dict:
    tpl = repo.publish_template_draft(
        db, template_id, (payload.note if payload else "")
    )
    if tpl is None:
        raise HTTPException(status_code=404, detail="没有可发布的草稿")
    return _template_out(db, tpl)


@templates_router.delete("/templates/{template_id}/draft", response_model=TemplateOut)
def discard_template_draft(
    template_id: int, db: Session = Depends(get_db)
) -> dict:
    tpl = repo.discard_template_draft(db, template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="模板不存在或为内置模板")
    return _template_out(db, tpl)


@templates_router.post("/templates/{template_id}/clone", response_model=TemplateOut)
def clone_template(template_id: int, db: Session = Depends(get_db)) -> dict:
    """克隆任意模板（含内置）为可编辑的自定义模板。"""
    tpl = repo.get_template(db, template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    clone = repo.create_template(
        db,
        name=f"{tpl.name} 副本"[:100],
        content=tpl.content,
        description=tpl.description or "",
        category=tpl.category or "general",
    )
    return _template_out(db, clone)


@templates_router.get("/templates/default", response_model=TemplateOut)
def get_default_template(db: Session = Depends(get_db)) -> dict:
    """隐藏的默认模板：只读查看，供管理面审计。"""
    tpl = repo.get_template_by_system_key(db, "default")
    if tpl is None:
        raise HTTPException(status_code=404, detail="默认模板不存在")
    return _template_out(db, tpl)


@templates_router.delete("/templates/{template_id}", status_code=204)
def delete_template(template_id: int, db: Session = Depends(get_db)) -> None:
    """删除自定义模板（内置模板不可删除）。"""
    if not repo.delete_template(db, template_id):
        raise HTTPException(status_code=404, detail="模板不存在或为内置模板")


@templates_router.get("/templates/{template_id}/versions")
def list_template_versions(
    template_id: int,
    include_content: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> dict:
    """模板版本列表（最新在前；include_content=false 时只返回元数据）。"""
    if repo.get_template(db, template_id) is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return {
        "versions": repo.list_template_versions(
            db, template_id, include_content=include_content
        )
    }


@templates_router.get("/templates/{template_id}/versions/{version_id}")
def get_template_version(
    template_id: int, version_id: int, db: Session = Depends(get_db)
) -> dict:
    version = repo.get_template_version(db, template_id, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    return repo._template_version_to_dict(version)


@templates_router.post("/templates/{template_id}/restore")
def restore_template_version(
    template_id: int,
    payload: PromptVersionRestore,
    db: Session = Depends(get_db),
) -> dict:
    """恢复旧版本（以旧内容新建一个版本；内置模板只读）。"""
    tpl = repo.get_template(db, template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    if tpl.is_system:
        raise HTTPException(status_code=400, detail="内置模板不可恢复，请新建自定义模板")
    version, tpl = repo.restore_template_version(
        db, template_id, payload.version_id, note=payload.note
    )
    if version is None or tpl is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    versions = repo.list_template_versions(db, template_id)
    restored = next((v for v in versions if v["id"] == version.id), None)
    return {"ok": True, "version": restored, "template": _template_out(db, tpl)}


def _fragment_out(db: Session, key: str) -> dict | None:
    for item in repo.list_prompt_fragments(db):
        if item["key"] == key:
            return item
    return None


@fragments_router.get("/prompts/fragments")
def list_prompt_fragments(db: Session = Depends(get_db)) -> dict:
    """全部系统提示词片段（当前内容 + 版本 + 是否自定义）。"""
    return {"fragments": repo.list_prompt_fragments(db)}


@fragments_router.patch("/prompts/fragments/{key}")
def update_prompt_fragment(
    key: str,
    payload: PromptFragmentUpdate,
    db: Session = Depends(get_db),
) -> dict:
    """发布片段新版本（内容变化才新增版本）。"""
    spec = FRAGMENTS_BY_KEY.get(key)
    if spec is None:
        raise HTTPException(status_code=404, detail="片段不存在")
    if not spec.get("editable", True):
        raise HTTPException(status_code=400, detail="该片段不允许编辑")
    try:
        validate_fragment_content(key, payload.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    fragment = repo.publish_prompt_fragment(
        db, key, payload.content, note=payload.note
    )
    if fragment is None:
        raise HTTPException(status_code=404, detail="片段不存在")
    return {
        "ok": True,
        "fragment": _fragment_out(db, key),
        "warnings": lint_fragment_content(key, payload.content),
    }


@fragments_router.put("/prompts/fragments/{key}/draft")
def save_fragment_draft(
    key: str, payload: PromptDraftSave, db: Session = Depends(get_db)
) -> dict:
    if key not in FRAGMENTS_BY_KEY:
        raise HTTPException(status_code=404, detail="片段不存在")
    fragment = repo.save_prompt_fragment_draft(
        db, key, payload.content, note=payload.note
    )
    if fragment is None:
        raise HTTPException(status_code=404, detail="片段不存在")
    return {"ok": True, "fragment": _fragment_out(db, key)}


@fragments_router.post("/prompts/fragments/{key}/publish-draft")
def publish_fragment_draft(
    key: str,
    payload: PromptDraftPublish | None = None,
    db: Session = Depends(get_db),
) -> dict:
    fragment = repo.get_prompt_fragment(db, key)
    if fragment is None or not fragment.draft_content:
        raise HTTPException(status_code=404, detail="没有可发布的草稿")
    try:
        validate_fragment_content(key, fragment.draft_content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    fragment = repo.publish_prompt_fragment_draft(
        db, key, note=(payload.note if payload else "")
    )
    if fragment is None:
        raise HTTPException(status_code=404, detail="没有可发布的草稿")
    return {
        "ok": True,
        "fragment": _fragment_out(db, key),
        "warnings": lint_fragment_content(key, fragment.content),
    }


@fragments_router.delete("/prompts/fragments/{key}/draft")
def discard_fragment_draft(
    key: str, db: Session = Depends(get_db)
) -> dict:
    if key not in FRAGMENTS_BY_KEY:
        raise HTTPException(status_code=404, detail="片段不存在")
    fragment = repo.discard_prompt_fragment_draft(db, key)
    if fragment is None:
        raise HTTPException(status_code=404, detail="片段不存在")
    return {"ok": True, "fragment": _fragment_out(db, key)}


@fragments_router.post("/prompts/fragments/{key}/lint")
def lint_prompt_fragment(key: str, payload: PromptLintRequest) -> dict:
    """发布前检查：硬错误（无法保存）+ 提示（可确认后保存）。"""
    if key not in FRAGMENTS_BY_KEY:
        raise HTTPException(status_code=404, detail="片段不存在")
    errors: list[str] = []
    try:
        validate_fragment_content(key, payload.content)
    except ValueError as exc:
        errors.append(str(exc))
    warnings = lint_fragment_content(key, payload.content)
    return {"ok": not errors, "errors": errors, "warnings": warnings}


@fragments_router.post("/prompts/normalize")
def normalize_prompt(payload: PromptLintRequest) -> dict:
    """把句中硬换行合并回一行；只返回新内容，不落库。"""
    normalized = normalize_mid_sentence_breaks(payload.content)
    return {
        "content": normalized,
        "changed": normalized != payload.content,
        "remaining_breaks": find_mid_sentence_breaks(normalized),
    }


@fragments_router.get("/prompts/fragments/{key}/versions")
def list_prompt_fragment_versions(
    key: str,
    include_content: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> dict:
    if key not in FRAGMENTS_BY_KEY:
        raise HTTPException(status_code=404, detail="片段不存在")
    return {
        "versions": repo.list_prompt_fragment_versions(
            db, key, include_content=include_content
        )
    }


@fragments_router.get("/prompts/fragments/{key}/versions/{version_id}")
def get_prompt_fragment_version(
    key: str, version_id: int, db: Session = Depends(get_db)
) -> dict:
    version = repo.get_prompt_fragment_version(db, key, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    return {
        "id": version.id,
        "fragment_id": version.fragment_id,
        "version_no": version.version_no,
        "content": version.content,
        "content_hash": version.content_hash,
        "note": version.note or "",
        "chars": len(version.content or ""),
        "created_at": version.created_at,
    }


@fragments_router.post("/prompts/fragments/{key}/restore")
def restore_prompt_fragment(
    key: str,
    payload: PromptVersionRestore,
    db: Session = Depends(get_db),
) -> dict:
    if key not in FRAGMENTS_BY_KEY:
        raise HTTPException(status_code=404, detail="片段不存在")
    fragment = repo.restore_prompt_fragment_version(
        db, key, payload.version_id, note=payload.note
    )
    if fragment is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    return {"ok": True, "fragment": _fragment_out(db, key)}


@fragments_router.post("/prompts/fragments/{key}/reset")
def reset_prompt_fragment(
    key: str,
    payload: PromptFragmentUpdate | None = None,
    db: Session = Depends(get_db),
) -> dict:
    if key not in FRAGMENTS_BY_KEY:
        raise HTTPException(status_code=404, detail="片段不存在")
    fragment = repo.reset_prompt_fragment(
        db, key, note=(payload.note if payload else "")
    )
    if fragment is None:
        raise HTTPException(status_code=404, detail="片段不存在")
    return {"ok": True, "fragment": _fragment_out(db, key)}


def _eval_questions(limit: int) -> list[dict]:
    path = Path(__file__).resolve().parents[2] / "eval_questions.json"
    try:
        import json as _json

        data = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = []
    return [item for item in data if isinstance(item, dict)][:limit]


def _system_prompt_for_version(db: Session, tpl, version_id: int) -> str:
    version = repo.get_template_version(db, tpl.id, version_id)
    if version is None:
        raise ValueError("模板版本不存在")
    fragments = {
        item["key"]: item["content"]
        for item in repo.list_prompt_fragments(db)
    }
    return compose_system_prompt(
        version.content, static_only=True, fragments=fragments
    )


def _invoke_prompt(agent, system_prompt: str, question: str) -> dict:
    started = time.perf_counter()
    response = agent.chat.invoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(content=question),
        ]
    )
    text = message_text(response.content).strip()
    return {
        "text": text,
        "chars": len(text),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": getattr(response, "usage_metadata", None) or {},
    }


@trials_router.post("/prompts/trials/ab")
def run_ab_trial(
    payload: PromptTrialABRequest,
    db: Session = Depends(get_db),
    agent=Depends(get_agent_service),
) -> dict:
    """同一问题分别用两个模板版本试跑（不绑定工具，结果落库）。"""
    tpl = repo.get_template(db, payload.template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    try:
        system_a = _system_prompt_for_version(db, tpl, payload.version_a_id)
        system_b = _system_prompt_for_version(db, tpl, payload.version_b_id)
        result_a = _invoke_prompt(agent, system_a, payload.question)
        result_b = _invoke_prompt(agent, system_b, payload.question)
    except Exception as exc:
        repo.create_prompt_trial(
            db,
            kind="ab",
            template_id=payload.template_id,
            version_a_id=payload.version_a_id,
            version_b_id=payload.version_b_id,
            question=payload.question,
            status="error",
            error=str(exc),
        )
        raise HTTPException(status_code=502, detail=f"A/B 试跑失败：{exc}") from exc
    trial = repo.create_prompt_trial(
        db,
        kind="ab",
        template_id=payload.template_id,
        version_a_id=payload.version_a_id,
        version_b_id=payload.version_b_id,
        question=payload.question,
        result={"a": result_a, "b": result_b},
    )
    return {"ok": True, "trial": trial}


@trials_router.post("/prompts/trials/eval")
def run_eval_trial(
    payload: PromptTrialEvalRequest,
    db: Session = Depends(get_db),
    agent=Depends(get_agent_service),
) -> dict:
    """对指定模板版本跑轻量问题集（无工具、无裁判），用于版本冒烟。"""
    tpl = repo.get_template(db, payload.template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    version = repo.get_template_version(db, tpl.id, payload.version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="模板版本不存在")
    questions = _eval_questions(payload.limit)
    if not questions:
        raise HTTPException(status_code=400, detail="没有可用的评测问题")
    try:
        system_prompt = _system_prompt_for_version(
            db, tpl, payload.version_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    items = []
    for item in questions:
        question = str(item.get("question") or "")
        try:
            output = _invoke_prompt(agent, system_prompt, question)
            items.append(
                {
                    "id": item.get("id"),
                    "category": item.get("category"),
                    "question": question,
                    "expect": item.get("expect"),
                    "ok": True,
                    **output,
                }
            )
        except Exception as exc:
            items.append(
                {
                    "id": item.get("id"),
                    "category": item.get("category"),
                    "question": question,
                    "ok": False,
                    "error": str(exc),
                }
            )
    trial = repo.create_prompt_trial(
        db,
        kind="eval",
        template_id=payload.template_id,
        version_a_id=payload.version_id,
        question=f"评测 {len(items)} 题",
        result={"version_no": version.version_no, "items": items},
    )
    return {"ok": True, "trial": trial}


@trials_router.get("/prompts/trials")
def list_trials(
    kind: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    return {"trials": repo.list_prompt_trials(db, kind=kind, limit=limit)}


def _impact_rows(db: Session, limit: int = 200) -> list[dict]:
    rows: list[dict] = []
    for item in repo.list_conversations(db, limit=limit):
        if not item.get("message_count"):
            continue
        status = conversation_prompt_status(db, item["id"])
        if not status.get("upgradable"):
            continue
        tpl = (
            repo.get_template(db, item["template_id"])
            if item.get("template_id")
            else None
        )
        locked = status.get("locked") or {}
        latest = status.get("latest") or {}
        rows.append(
            {
                "id": item["id"],
                "title": item["title"],
                "template_id": item.get("template_id"),
                "template_name": tpl.name if tpl else "默认助手（内置）",
                "message_count": item.get("message_count", 0),
                "updated_at": item.get("updated_at"),
                "locked": bool(locked),
                "locked_template_version_no": locked.get(
                    "template_version_no"
                ),
                "latest_template_version_no": latest.get(
                    "template_version_no"
                ),
                "outdated_units": status.get("outdated_units", []),
                "outdated_details": status.get("outdated_details", []),
            }
        )
    return rows


@audit_router.get("/prompts/audit")
def audit_prompts(db: Session = Depends(get_db)) -> dict:
    """全量体检：模板（含隐藏默认）+ 片段，返回发布前提示。"""
    templates = repo.list_templates(db)
    hidden = repo.get_template_by_system_key(db, "default")
    if hidden is not None:
        versions = repo.list_template_versions(db, hidden.id)
        templates = [
            *templates,
            {
                "id": hidden.id,
                "name": hidden.name,
                "content": hidden.content,
                "is_system": True,
                "latest_version_no": versions[0]["version_no"] if versions else None,
            },
        ]

    template_issues = []
    for item in templates:
        warnings = lint_template_content(item.get("content") or "")
        if warnings:
            template_issues.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name") or "",
                    "version_no": item.get("latest_version_no"),
                    "warnings": warnings,
                }
            )

    fragment_issues = []
    for item in repo.list_prompt_fragments(db):
        warnings = lint_fragment_content(
            item["key"], item.get("content") or ""
        )
        if warnings:
            fragment_issues.append(
                {
                    "key": item["key"],
                    "title": item.get("title") or item["key"],
                    "version_no": item.get("version_no"),
                    "warnings": warnings,
                }
            )

    return {
        "ok": not template_issues and not fragment_issues,
        "templates": template_issues,
        "fragments": fragment_issues,
        "issue_count": len(template_issues) + len(fragment_issues),
    }


@audit_router.get("/prompts/impact")
def prompt_impact(
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict:
    """锁定旧版本的会话列表（只含有消息的会话）。"""
    rows = _impact_rows(db, limit=limit)
    return {"count": len(rows), "conversations": rows}


@audit_router.post("/prompts/apply-latest-batch")
def apply_latest_batch(
    payload: PromptBatchApply,
    db: Session = Depends(get_db),
    agent=Depends(get_agent_service),
) -> dict:
    """批量升级会话到最新提示词；逐会话跳过运行中/挂起中的会话。"""
    ids = payload.conversation_ids
    if not ids:
        ids = [row["id"] for row in _impact_rows(db, limit=1000)]
    applied: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    for conversation_id in list(dict.fromkeys(ids))[:1000]:
        if is_conversation_active(conversation_id):
            skipped.append(
                {"conversation_id": conversation_id, "reason": "会话正在运行"}
            )
            continue
        try:
            pending = (
                agent.pending_state(conversation_id) if agent is not None else {}
            )
        except Exception:
            pending = {}
        if pending.get("pending") or pending.get("interrupted"):
            skipped.append(
                {"conversation_id": conversation_id, "reason": "有挂起或未完成任务"}
            )
            continue
        result = apply_latest(db, conversation_id)
        if result.get("ok"):
            applied.append(
                {
                    "conversation_id": conversation_id,
                    "cache_break": result.get("cache_break"),
                    "first_lock": result.get("first_lock"),
                }
            )
        else:
            failed.append(
                {
                    "conversation_id": conversation_id,
                    "error": result.get("error") or "升级失败",
                }
            )
    return {
        "applied": applied,
        "skipped": skipped,
        "failed": failed,
        "applied_count": len(applied),
    }


@audit_router.post("/conversations/{conversation_id}/prompt/pin")
def pin_conversation_version(
    conversation_id: int,
    payload: PromptPinRequest,
    db: Session = Depends(get_db),
    agent=Depends(get_agent_service),
) -> dict:
    """把会话锁定到指定模板版本（显式操作，会断一次前缀缓存）。"""
    if is_conversation_active(conversation_id):
        raise HTTPException(status_code=409, detail="会话正在运行，先停止后再锁定")
    try:
        pending = (
            agent.pending_state(conversation_id) if agent is not None else {}
        )
    except Exception:
        pending = {}
    if pending.get("pending") or pending.get("interrupted"):
        raise HTTPException(
            status_code=409, detail="会话有挂起或未完成任务，先处理后再锁定"
        )
    result = pin_conversation_prompt(
        db,
        conversation_id,
        payload.version_id,
        payload.fragment_version_ids,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "锁定失败")
    return {**result, "message": "已锁定到指定版本；下一轮请求前缀缓存失效一次。"}


@audit_router.get("/prompts/export")
def export_prompts(db: Session = Depends(get_db)) -> dict:
    """导出模板、片段及其版本为 JSON（含隐藏默认模板）。"""
    templates = []
    for item in repo.list_templates(db):
        templates.append(
            {
                **item,
                "versions": repo.list_template_versions(db, item["id"]),
            }
        )
    fragments = []
    for item in repo.list_prompt_fragments(db):
        fragments.append(
            {
                **item,
                "versions": repo.list_prompt_fragment_versions(db, item["key"]),
            }
        )
    default_template = None
    hidden = repo.get_template_by_system_key(db, "default")
    if hidden is not None:
        default_template = {
            "name": hidden.name,
            "description": hidden.description,
            "category": hidden.category,
            "content": hidden.content,
            "versions": repo.list_template_versions(db, hidden.id),
        }
    return {
        "version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "templates": templates,
        "fragments": fragments,
        "default_template": default_template,
    }


@audit_router.post("/prompts/import")
def import_prompts(
    payload: PromptImportRequest, db: Session = Depends(get_db)
) -> dict:
    """导入模板（创建为自定义副本）与片段（追加新版本）。"""
    data = payload.data or {}
    templates = data.get("templates") or []
    fragments = data.get("fragments") or []
    if not isinstance(templates, list) or not isinstance(fragments, list):
        raise HTTPException(status_code=400, detail="导入格式不正确")
    if len(templates) > 200 or len(fragments) > 200:
        raise HTTPException(status_code=400, detail="导入条目过多")

    imported_templates = []
    for item in templates:
        if not isinstance(item, dict) or not str(item.get("content") or "").strip():
            continue
        name = str(item.get("name") or "导入模板")
        if not name.endswith("（导入）"):
            name = f"{name}（导入）"
        tpl = repo.import_template_bundle(
            db,
            name=name,
            description=str(item.get("description") or ""),
            category=str(item.get("category") or "general"),
            content=str(item.get("content") or ""),
            archived=bool(item.get("archived")),
            versions=item.get("versions") if isinstance(item.get("versions"), list) else None,
        )
        imported_templates.append({"id": tpl.id, "name": tpl.name})

    imported_fragments = []
    skipped_fragments = []
    for item in fragments:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        content = str(item.get("content") or "")
        if key not in FRAGMENTS_BY_KEY or not content.strip():
            skipped_fragments.append(key or "(空)")
            continue
        try:
            validate_fragment_content(key, content)
        except ValueError:
            skipped_fragments.append(key)
            continue
        repo.publish_prompt_fragment(db, key, content, note="导入")
        imported_fragments.append(key)

    return {
        "ok": True,
        "templates": imported_templates,
        "fragments": imported_fragments,
        "skipped_fragments": skipped_fragments,
    }


@audit_router.post("/prompts/preview")
def preview_prompt(
    payload: PromptPreviewRequest,
    db: Session = Depends(get_db),
    agent=Depends(get_agent_service),
) -> dict:
    """只读预览最终系统提示词：不写会话锁，不改版本。

    静态核心来自模板 + 全局约束 + 当前工具规则；动态块按请求开关取
    技能目录/知识库清单/任务清单。未保存的模板或片段内容可直接覆盖预览。
    """
    resolution = preview_prompt_resolution(
        db,
        conversation_id=payload.conversation_id,
        template_id=payload.template_id,
        template_content=payload.template_content,
        fragment_key=payload.fragment_key,
        fragment_content=payload.fragment_content,
    )
    settings = getattr(agent, "settings", None)
    fragments = {
        item["key"]: item["content"] for item in resolution["fragments"]
    }

    skills_catalog = ""
    skills_meta: dict = {}
    if payload.include_live_context and effective_or(
        settings, "skills_enabled", True
    ):
        try:
            from ..skills import build_skill_index, load_prefs, scan_skills

            prefs = load_prefs(db) or {}
            enabled = set(prefs.get("enabled") or [])
            if enabled:
                scanned = scan_skills()
                entries = [s for s in scanned if s["name"] in enabled]
                budget = int(
                    effective_or(
                        settings, "skills_index_max_chars", 12000
                    )
                    or 12000
                )
                index = build_skill_index(entries, budget=budget)
                skills_catalog = index.text
                skills_meta = {
                    "visible": index.visible,
                    "names_only": index.names_only,
                    "chars": index.chars,
                }
        except Exception:
            skills_catalog = ""
            skills_meta = {}

    kb_documents: list[str] = []
    kb_total = 0
    if payload.include_live_context and payload.use_knowledge_base:
        try:
            rag = getattr(agent, "rag", None)
            documents = rag.list_documents() if rag is not None else []
            kb_total = len(documents)
            kb_documents = [item["name"] for item in documents][:30]
        except Exception:
            kb_documents = []
            kb_total = 0

    todos_text = ""
    if payload.conversation_id:
        try:
            todos_text = todos_to_text(
                load_todos(db, payload.conversation_id)
            )
        except Exception:
            todos_text = ""

    common = dict(
        template_content=resolution["template"]["content"],
        use_knowledge_base=payload.use_knowledge_base,
        use_web_search=payload.use_web_search,
        advanced_tools=payload.advanced_tools,
        plan_only=payload.plan_only,
        fragments=fragments,
    )
    static_core = compose_system_prompt(static_only=True, **common)
    pre_history = compose_system_prompt(
        dynamic_only=True,
        kb_documents=kb_documents,
        kb_total=kb_total,
        skills_catalog=skills_catalog,
        **common,
    )
    post_history = compose_system_prompt(
        dynamic_only=True,
        todos_text=todos_text,
        **common,
    )

    warnings = lint_template_content(resolution["template"]["content"])
    if payload.fragment_key and payload.fragment_content is not None:
        warnings.extend(
            lint_fragment_content(
                payload.fragment_key, payload.fragment_content
            )
        )

    return {
        **resolution,
        "static_core": static_core,
        "pre_history": pre_history,
        "post_history": post_history,
        "chars": {
            "static_core": len(static_core),
            "pre_history": len(pre_history),
            "post_history": len(post_history),
        },
        "skills_index": skills_meta,
        "kb_total": kb_total,
        "kb_listed": len(kb_documents),
        "todos_present": bool(todos_text),
        "warnings": warnings,
        "note": (
            "预览不写会话锁；工具定义（bind_tools）与历史消息不在此展示。"
            "动态块按当前开关与已有数据生成。"
        ),
    }


@audit_router.get("/conversations/{conversation_id}/prompt")
def get_conversation_prompt(
    conversation_id: int, db: Session = Depends(get_db)
) -> dict:
    """会话锁定版本 vs 最新版本（含可升级片段列表）。"""
    if repo.get_conversation(db, conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conversation_prompt_status(db, conversation_id)


@audit_router.post("/conversations/{conversation_id}/prompt/apply-latest")
def apply_conversation_prompt_latest(
    conversation_id: int,
    db: Session = Depends(get_db),
    agent=Depends(get_agent_service),
) -> dict:
    """把会话升级到最新提示词（会显式断一次前缀缓存）。"""
    if repo.get_conversation(db, conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if is_conversation_active(conversation_id):
        raise HTTPException(status_code=409, detail="会话正在运行，先停止后再升级")
    try:
        pending = agent.pending_state(conversation_id) if agent is not None else {}
    except Exception:
        pending = {}
    if pending.get("pending") or pending.get("interrupted"):
        raise HTTPException(
            status_code=409, detail="会话有挂起或未完成任务，先处理后再升级"
        )
    result = apply_latest(db, conversation_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "升级失败")
    if result.get("first_lock"):
        message = "已锁定最新提示词版本；后续编辑只影响新会话。"
    elif result.get("cache_break"):
        message = (
            "已应用最新提示词版本；下一轮请求会改变系统提示词，"
            "该会话的前缀缓存将失效一次。"
        )
    else:
        message = "会话已是最新提示词版本，无需升级。"
    return {**result, "message": message}


router = APIRouter()
router.include_router(templates_router)
router.include_router(fragments_router)
router.include_router(trials_router)
router.include_router(audit_router)
