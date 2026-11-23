"""提示词模板/片段/试验与会话锁定数据访问。"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..models import (
    Conversation,
    PromptFragment,
    PromptFragmentVersion,
    PromptTemplate,
    PromptTemplateVersion,
    PromptTrial,
)
from ..sanitize import sanitize_sources, sanitize_token_usage, truncate_tool_trace
from .common import _json_loads, content_hash

logger = logging.getLogger(__name__)


def get_template(db: Session, template_id: int) -> PromptTemplate | None:
    return db.get(PromptTemplate, template_id)


def get_template_by_system_key(
    db: Session, system_key: str
) -> PromptTemplate | None:
    return (
        db.execute(
            select(PromptTemplate).where(PromptTemplate.system_key == system_key)
        )
        .scalars()
        .first()
    )


def _latest_template_version(
    db: Session, template_id: int
) -> PromptTemplateVersion | None:
    return (
        db.execute(
            select(PromptTemplateVersion)
            .where(PromptTemplateVersion.template_id == template_id)
            .order_by(PromptTemplateVersion.version_no.desc())
        )
        .scalars()
        .first()
    )


def _ensure_template_version(
    db: Session, tpl: PromptTemplate, note: str = ""
) -> tuple[PromptTemplateVersion, bool]:
    """确保当前 tpl.content 有对应版本行；返回 (最新版本, 是否新建)。"""
    latest = _latest_template_version(db, tpl.id)
    current_hash = content_hash(tpl.content)
    if latest is None:
        latest = PromptTemplateVersion(
            template_id=tpl.id,
            version_no=1,
            content=tpl.content,
            content_hash=current_hash,
            note=note or "初始版本",
        )
        db.add(latest)
        db.flush()
        return latest, True
    if latest.content_hash != current_hash:
        latest = PromptTemplateVersion(
            template_id=tpl.id,
            version_no=latest.version_no + 1,
            content=tpl.content,
            content_hash=current_hash,
            note=note or "同步当前内容",
        )
        db.add(latest)
        db.flush()
        return latest, True
    return latest, False


def _append_template_version(
    db: Session,
    tpl: PromptTemplate,
    content: str,
    note: str = "",
) -> tuple[PromptTemplateVersion, bool]:
    """先把当前内容补成版本，再按需追加新版本；不 commit。"""
    latest, _ = _ensure_template_version(db, tpl)
    new_hash = content_hash(content)
    if latest.content_hash == new_hash:
        tpl.content = content
        return latest, False
    version = PromptTemplateVersion(
        template_id=tpl.id,
        version_no=latest.version_no + 1,
        content=content,
        content_hash=new_hash,
        note=note or "",
    )
    db.add(version)
    db.flush()
    tpl.content = content
    return version, True


def publish_template_content(
    db: Session, tpl: PromptTemplate, content: str, note: str = ""
) -> PromptTemplateVersion:
    version, _ = _append_template_version(db, tpl, content, note)
    db.commit()
    db.refresh(version)
    db.refresh(tpl)
    return version


def _template_version_to_dict(
    version: PromptTemplateVersion, include_content: bool = True
) -> dict:
    return {
        "id": version.id,
        "template_id": version.template_id,
        "version_no": version.version_no,
        "content": (version.content or "") if include_content else "",
        "content_omitted": not include_content,
        "content_hash": version.content_hash,
        "note": version.note or "",
        "chars": len(version.content or ""),
        "created_at": version.created_at,
    }


def list_templates(db: Session) -> list[dict]:
    rows = (
        db.execute(
            select(PromptTemplate)
            .where(
                (PromptTemplate.system_key.is_(None))
                | (PromptTemplate.system_key != "default")
            )
            .order_by(PromptTemplate.is_system.desc(), PromptTemplate.id)
        )
        .scalars()
        .all()
    )
    conversation_counts = dict(
        db.execute(
            select(Conversation.template_id, func.count())
            .where(Conversation.template_id.isnot(None))
            .group_by(Conversation.template_id)
        ).all()
    )
    out: list[dict] = []
    for t in rows:
        latest = _latest_template_version(db, t.id)
        out.append(
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "category": t.category,
                "content": t.content,
                "is_system": t.is_system,
                "system_key": t.system_key,
                "archived": bool(t.archived),
                "draft_content": t.draft_content,
                "draft_note": t.draft_note or "",
                "draft_updated_at": t.draft_updated_at,
                "has_draft": bool(t.draft_content),
                "content_hash": content_hash(t.content),
                "latest_version_id": latest.id if latest else None,
                "latest_version_no": latest.version_no if latest else None,
                "version_count": int(
                    db.scalar(
                        select(func.count())
                        .select_from(PromptTemplateVersion)
                        .where(PromptTemplateVersion.template_id == t.id)
                    )
                    or 0
                ),
                "conversation_count": int(conversation_counts.get(t.id) or 0),
                "created_at": t.created_at,
                "updated_at": t.updated_at,
            }
        )
    return out


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
    db.flush()
    _ensure_template_version(db, tpl, note="初始版本")
    db.commit()
    db.refresh(tpl)
    return tpl


def import_template_bundle(
    db: Session,
    *,
    name: str,
    description: str = "",
    category: str = "general",
    content: str,
    archived: bool = False,
    versions: list[dict] | None = None,
) -> PromptTemplate:
    """导入模板及其历史版本（始终创建为自定义模板，不覆盖内置）。"""
    rows = sorted(
        (versions or []),
        key=lambda item: int(item.get("version_no") or 0),
    )
    latest_content = rows[-1].get("content") if rows else content
    latest_content = latest_content or content
    tpl = PromptTemplate(
        name=name.strip()[:100] or "导入模板",
        description=(description or "").strip()[:255],
        category=(category or "general").strip()[:50] or "general",
        content=latest_content,
        is_system=False,
        archived=bool(archived),
    )
    db.add(tpl)
    db.flush()
    next_no = 1
    seen_hashes = set()
    for item in rows:
        text = str(item.get("content") or "")
        if not text.strip():
            continue
        h = content_hash(text)
        version_no = int(item.get("version_no") or next_no)
        if version_no < next_no:
            version_no = next_no
        if (version_no, h) in seen_hashes:
            continue
        seen_hashes.add((version_no, h))
        db.add(
            PromptTemplateVersion(
                template_id=tpl.id,
                version_no=version_no,
                content=text,
                content_hash=h,
                note=str(item.get("note") or "导入")[:500],
            )
        )
        next_no = version_no + 1
    _ensure_template_version(db, tpl, note="导入补版本")
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
    version_note: str = "",
) -> PromptTemplate | None:
    tpl = db.get(PromptTemplate, template_id)
    if tpl is None:
        return None
    if name is not None:
        tpl.name = name.strip()[:100]
    if description is not None:
        tpl.description = description.strip()[:255]
    if category is not None:
        tpl.category = category.strip()[:50] or "general"
    if content is not None:
        _append_template_version(db, tpl, content, version_note)
        tpl.draft_content = None
        tpl.draft_note = ""
        tpl.draft_updated_at = None
    db.commit()
    db.refresh(tpl)
    return tpl


def archive_template(db: Session, template_id: int, archived: bool = True):
    """归档/恢复自定义模板；内置模板不允许归档。"""
    tpl = db.get(PromptTemplate, template_id)
    if tpl is None or tpl.is_system:
        return None
    tpl.archived = bool(archived)
    db.commit()
    db.refresh(tpl)
    return tpl


def save_template_draft(
    db: Session, template_id: int, content: str, note: str = ""
):
    tpl = db.get(PromptTemplate, template_id)
    if tpl is None or tpl.is_system:
        return None
    tpl.draft_content = content
    tpl.draft_note = note or ""
    tpl.draft_updated_at = datetime.now()
    db.commit()
    db.refresh(tpl)
    return tpl


def publish_template_draft(db: Session, template_id: int, note: str = ""):
    tpl = db.get(PromptTemplate, template_id)
    if tpl is None or tpl.is_system or not tpl.draft_content:
        return None
    content = tpl.draft_content
    draft_note = note or tpl.draft_note or "发布草稿"
    _append_template_version(db, tpl, content, draft_note)
    tpl.draft_content = None
    tpl.draft_note = ""
    tpl.draft_updated_at = None
    db.commit()
    db.refresh(tpl)
    return tpl


def discard_template_draft(db: Session, template_id: int):
    tpl = db.get(PromptTemplate, template_id)
    if tpl is None or tpl.is_system:
        return None
    tpl.draft_content = None
    tpl.draft_note = ""
    tpl.draft_updated_at = None
    db.commit()
    db.refresh(tpl)
    return tpl


def delete_template(db: Session, template_id: int) -> bool:
    tpl = db.get(PromptTemplate, template_id)
    if tpl is None or tpl.is_system:
        return False
    # SQLite 测试库不强制 FK CASCADE，显式清理；被会话引用的绑定一并解绑，
    # 避免悬空 template_id 让下一轮解析回退默认提示词却无提示。
    db.execute(
        delete(PromptTemplateVersion).where(
            PromptTemplateVersion.template_id == template_id
        )
    )
    db.execute(
        update(Conversation)
        .where(Conversation.template_id == template_id)
        .values(
            template_id=None,
            prompt_bundle=None,
            prompt_bundle_hash=None,
            prompt_locked_at=None,
        )
    )
    db.delete(tpl)
    db.commit()
    return True


def list_template_versions(
    db: Session, template_id: int, include_content: bool = True
) -> list[dict]:
    rows = (
        db.execute(
            select(PromptTemplateVersion)
            .where(PromptTemplateVersion.template_id == template_id)
            .order_by(PromptTemplateVersion.version_no.desc())
        )
        .scalars()
        .all()
    )
    return [
        _template_version_to_dict(v, include_content=include_content)
        for v in rows
    ]


def get_template_version(
    db: Session, template_id: int, version_id: int
) -> PromptTemplateVersion | None:
    version = db.get(PromptTemplateVersion, version_id)
    if version is None or version.template_id != template_id:
        return None
    return version


def restore_template_version(
    db: Session,
    template_id: int,
    version_id: int,
    note: str = "",
) -> tuple[PromptTemplateVersion | None, PromptTemplate | None]:
    tpl = db.get(PromptTemplate, template_id)
    target = get_template_version(db, template_id, version_id)
    if tpl is None or target is None:
        return None, tpl
    version = publish_template_content(
        db, tpl, target.content, note or f"恢复自 v{target.version_no}"
    )
    return version, tpl


def seed_templates(db: Session) -> None:
    """幂等写入/同步内置提示词模板与版本。

    系统模板按 system_key 定位（旧库按 name 回填），内容变化才新增版本；
    用户自建模板不受影响。单个模板失败只告警，不阻断启动。
    """
    from ...agent.prompts import ALL_SYSTEM_TEMPLATES

    for preset in ALL_SYSTEM_TEMPLATES:
        try:
            key = preset["system_key"]
            row = get_template_by_system_key(db, key)
            if row is None:
                # 兼容旧库：按名称找到系统模板并回填 system_key
                row = (
                    db.execute(
                        select(PromptTemplate)
                        .where(
                            PromptTemplate.name == preset["name"],
                            PromptTemplate.is_system.is_(True),
                        )
                        .order_by(PromptTemplate.id)
                    )
                    .scalars()
                    .first()
                )
                if row is not None and not row.system_key:
                    row.system_key = key
            if row is None:
                row = PromptTemplate(
                    name=preset["name"],
                    description=preset["description"],
                    category=preset["category"],
                    content=preset["content"],
                    is_system=True,
                    system_key=key,
                )
                db.add(row)
                db.flush()
                _ensure_template_version(db, row, note="初始版本")
                db.commit()
                continue
            if not row.is_system:
                logger.warning(
                    "系统提示词模板 %s 与用户模板同名，跳过覆盖", preset["name"]
                )
                continue
            row.description = preset["description"]
            row.category = preset["category"]
            row.system_key = key
            _ensure_template_version(db, row)
            if row.content != preset["content"]:
                _append_template_version(
                    db, row, preset["content"], note="代码内置模板更新"
                )
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("seed 提示词模板 %s 失败：%s", preset.get("name"), exc)

    # 兼容上线前已存在的自定义模板：补一个 v1，避免版本列表/锁定无版本可用
    try:
        rows = db.execute(select(PromptTemplate)).scalars().all()
        for tpl in rows:
            if _latest_template_version(db, tpl.id) is None:
                _ensure_template_version(db, tpl, note="初始化版本")
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("提示词模板版本回填失败：%s", exc)


def _latest_fragment_version(
    db: Session, fragment_id: int
) -> PromptFragmentVersion | None:
    return (
        db.execute(
            select(PromptFragmentVersion)
            .where(PromptFragmentVersion.fragment_id == fragment_id)
            .order_by(PromptFragmentVersion.version_no.desc())
        )
        .scalars()
        .first()
    )


def _ensure_fragment_version(
    db: Session, fragment: PromptFragment, note: str = ""
) -> tuple[PromptFragmentVersion, bool]:
    latest = _latest_fragment_version(db, fragment.id)
    current_hash = content_hash(fragment.content)
    if latest is None:
        latest = PromptFragmentVersion(
            fragment_id=fragment.id,
            version_no=1,
            content=fragment.content,
            content_hash=current_hash,
            note=note or "初始版本",
        )
        db.add(latest)
        db.flush()
        return latest, True
    if latest.content_hash != current_hash:
        latest = PromptFragmentVersion(
            fragment_id=fragment.id,
            version_no=latest.version_no + 1,
            content=fragment.content,
            content_hash=current_hash,
            note=note or "同步当前内容",
        )
        db.add(latest)
        db.flush()
        return latest, True
    return latest, False


def _append_fragment_version(
    db: Session,
    fragment: PromptFragment,
    content: str,
    note: str = "",
) -> tuple[PromptFragmentVersion, bool]:
    latest, _ = _ensure_fragment_version(db, fragment)
    new_hash = content_hash(content)
    if latest.content_hash == new_hash:
        fragment.content = content
        return latest, False
    version = PromptFragmentVersion(
        fragment_id=fragment.id,
        version_no=latest.version_no + 1,
        content=content,
        content_hash=new_hash,
        note=note or "",
    )
    db.add(version)
    db.flush()
    fragment.content = content
    return version, True


def _fragment_version_to_dict(
    version: PromptFragmentVersion, include_content: bool = True
) -> dict:
    return {
        "id": version.id,
        "fragment_id": version.fragment_id,
        "version_no": version.version_no,
        "content": (version.content or "") if include_content else "",
        "content_omitted": not include_content,
        "content_hash": version.content_hash,
        "note": version.note or "",
        "chars": len(version.content or ""),
        "created_at": version.created_at,
    }


def get_prompt_fragment(db: Session, key: str) -> PromptFragment | None:
    return (
        db.execute(select(PromptFragment).where(PromptFragment.key == key))
        .scalars()
        .first()
    )


def list_prompt_fragments(db: Session) -> list[dict]:
    from ...agent.prompts import FRAGMENT_SPECS

    rows = {
        f.key: f
        for f in db.execute(select(PromptFragment)).scalars().all()
    }
    out: list[dict] = []
    for spec in FRAGMENT_SPECS:
        fragment = rows.get(spec["key"])
        latest = _latest_fragment_version(db, fragment.id) if fragment else None
        content = fragment.content if fragment else spec["default"]
        default_hash = content_hash(spec["default"])
        base_hash = fragment.base_default_hash if fragment else None
        out.append(
            {
                "key": spec["key"],
                "title": spec["title"],
                "description": spec["description"],
                "scope": spec["scope"],
                "position": spec.get("position", "pre_history"),
                "editable": bool(spec.get("editable", True)),
                "placeholder": spec.get("placeholder"),
                "content": content,
                "content_hash": content_hash(content),
                "default_hash": default_hash,
                "base_default_hash": base_hash,
                "default_changed": bool(
                    fragment
                    and fragment.is_customized
                    and base_hash
                    and base_hash != default_hash
                ),
                "is_customized": bool(fragment and fragment.is_customized),
                "draft_content": fragment.draft_content if fragment else None,
                "draft_note": (fragment.draft_note if fragment else "") or "",
                "draft_updated_at": (
                    fragment.draft_updated_at if fragment else None
                ),
                "has_draft": bool(fragment and fragment.draft_content),
                "version_id": latest.id if latest else None,
                "version_no": latest.version_no if latest else None,
                "version_count": (
                    int(
                        db.scalar(
                            select(func.count())
                            .select_from(PromptFragmentVersion)
                            .where(
                                PromptFragmentVersion.fragment_id == fragment.id
                            )
                        )
                        or 0
                    )
                    if fragment
                    else 0
                ),
                "updated_at": fragment.updated_at if fragment else None,
            }
        )
    return out


def seed_prompt_fragments(db: Session) -> None:
    """幂等同步片段默认值：未自定义的片段随代码升级，自定义内容保持。"""
    from ...agent.prompts import FRAGMENT_SPECS

    for spec in FRAGMENT_SPECS:
        try:
            fragment = get_prompt_fragment(db, spec["key"])
            default_hash = content_hash(spec["default"])
            if fragment is None:
                fragment = PromptFragment(
                    key=spec["key"],
                    title=spec["title"],
                    description=spec["description"],
                    scope=spec["scope"],
                    editable=bool(spec.get("editable", True)),
                    is_customized=False,
                    base_default_hash=default_hash,
                    content=spec["default"],
                )
                db.add(fragment)
                db.flush()
                _ensure_fragment_version(db, fragment, note="初始版本")
                db.commit()
                continue
            fragment.title = spec["title"]
            fragment.description = spec["description"]
            fragment.scope = spec["scope"]
            fragment.editable = bool(spec.get("editable", True))
            _ensure_fragment_version(db, fragment)
            if not fragment.is_customized and fragment.content != spec["default"]:
                _append_fragment_version(
                    db, fragment, spec["default"], note="代码内置片段更新"
                )
            fragment.is_customized = content_hash(fragment.content) != default_hash
            if not fragment.base_default_hash:
                # 旧库迁移：不知道自定义时基于哪版默认，取当前默认作为基线
                fragment.base_default_hash = default_hash
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("seed 提示词片段 %s 失败：%s", spec.get("key"), exc)


def publish_prompt_fragment(
    db: Session, key: str, content: str, note: str = ""
) -> PromptFragment | None:
    from ...agent.prompts import FRAGMENTS_BY_KEY

    fragment = get_prompt_fragment(db, key)
    spec = FRAGMENTS_BY_KEY.get(key)
    if fragment is None or spec is None:
        return None
    _append_fragment_version(db, fragment, content, note)
    default_hash = content_hash(spec["default"])
    fragment.is_customized = content_hash(content) != default_hash
    fragment.base_default_hash = default_hash
    fragment.draft_content = None
    fragment.draft_note = ""
    fragment.draft_updated_at = None
    db.commit()
    db.refresh(fragment)
    return fragment


def save_prompt_fragment_draft(
    db: Session, key: str, content: str, note: str = ""
) -> PromptFragment | None:
    fragment = get_prompt_fragment(db, key)
    if fragment is None:
        return None
    fragment.draft_content = content
    fragment.draft_note = note or ""
    fragment.draft_updated_at = datetime.now()
    db.commit()
    db.refresh(fragment)
    return fragment


def publish_prompt_fragment_draft(
    db: Session, key: str, note: str = ""
) -> PromptFragment | None:
    fragment = get_prompt_fragment(db, key)
    if fragment is None or not fragment.draft_content:
        return None
    return publish_prompt_fragment(
        db, key, fragment.draft_content, note or fragment.draft_note or "发布草稿"
    )


def discard_prompt_fragment_draft(
    db: Session, key: str
) -> PromptFragment | None:
    fragment = get_prompt_fragment(db, key)
    if fragment is None:
        return None
    fragment.draft_content = None
    fragment.draft_note = ""
    fragment.draft_updated_at = None
    db.commit()
    db.refresh(fragment)
    return fragment


def reset_prompt_fragment(
    db: Session, key: str, note: str = ""
) -> PromptFragment | None:
    from ...agent.prompts import FRAGMENTS_BY_KEY

    fragment = get_prompt_fragment(db, key)
    spec = FRAGMENTS_BY_KEY.get(key)
    if fragment is None or spec is None:
        return None
    _append_fragment_version(
        db, fragment, spec["default"], note or "重置为代码默认"
    )
    fragment.is_customized = False
    fragment.base_default_hash = content_hash(spec["default"])
    fragment.draft_content = None
    fragment.draft_note = ""
    fragment.draft_updated_at = None
    db.commit()
    db.refresh(fragment)
    return fragment


def list_prompt_fragment_versions(
    db: Session, key: str, include_content: bool = True
) -> list[dict]:
    fragment = get_prompt_fragment(db, key)
    if fragment is None:
        return []
    rows = (
        db.execute(
            select(PromptFragmentVersion)
            .where(PromptFragmentVersion.fragment_id == fragment.id)
            .order_by(PromptFragmentVersion.version_no.desc())
        )
        .scalars()
        .all()
    )
    return [
        _fragment_version_to_dict(v, include_content=include_content)
        for v in rows
    ]


def get_prompt_fragment_version(
    db: Session, key: str, version_id: int
) -> PromptFragmentVersion | None:
    fragment = get_prompt_fragment(db, key)
    if fragment is None:
        return None
    version = db.get(PromptFragmentVersion, version_id)
    if version is None or version.fragment_id != fragment.id:
        return None
    return version


def restore_prompt_fragment_version(
    db: Session, key: str, version_id: int, note: str = ""
) -> PromptFragment | None:
    from ...agent.prompts import FRAGMENTS_BY_KEY

    fragment = get_prompt_fragment(db, key)
    target = get_prompt_fragment_version(db, key, version_id)
    spec = FRAGMENTS_BY_KEY.get(key)
    if fragment is None or target is None or spec is None:
        return None
    _append_fragment_version(
        db,
        fragment,
        target.content,
        note or f"恢复自 v{target.version_no}",
    )
    default_hash = content_hash(spec["default"])
    fragment.is_customized = content_hash(fragment.content) != default_hash
    fragment.base_default_hash = default_hash
    fragment.draft_content = None
    fragment.draft_note = ""
    fragment.draft_updated_at = None
    db.commit()
    db.refresh(fragment)
    return fragment


def get_conversation_prompt_lock(conv: Conversation | None) -> dict | None:
    bundle = getattr(conv, "prompt_bundle", None)
    if conv is None or not bundle:
        return None
    return _json_loads(bundle)


def set_conversation_prompt_lock(
    db: Session, conv: Conversation, bundle: dict
) -> None:
    raw = json.dumps(bundle, ensure_ascii=False)
    conv.prompt_bundle = raw
    conv.prompt_bundle_hash = bundle.get("bundle_hash") or content_hash(raw)
    conv.prompt_locked_at = datetime.now()
    db.commit()
    db.refresh(conv)


def _prompt_trial_to_dict(row: PromptTrial) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "template_id": row.template_id,
        "version_a_id": row.version_a_id,
        "version_b_id": row.version_b_id,
        "question": row.question or "",
        "status": row.status,
        "result": _json_loads(row.result) if row.result else None,
        "error": row.error or "",
        "created_at": row.created_at,
    }


def create_prompt_trial(
    db: Session,
    *,
    kind: str,
    template_id: int | None = None,
    version_a_id: int | None = None,
    version_b_id: int | None = None,
    question: str = "",
    status: str = "done",
    result=None,
    error: str = "",
) -> dict:
    row = PromptTrial(
        kind=kind,
        template_id=template_id,
        version_a_id=version_a_id,
        version_b_id=version_b_id,
        question=question or "",
        status=status,
        result=json.dumps(result, ensure_ascii=False) if result is not None else None,
        error=error or "",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _prompt_trial_to_dict(row)


def list_prompt_trials(
    db: Session, kind: str | None = None, limit: int = 50
) -> list[dict]:
    query = select(PromptTrial).order_by(PromptTrial.id.desc()).limit(limit)
    if kind:
        query = (
            select(PromptTrial)
            .where(PromptTrial.kind == kind)
            .order_by(PromptTrial.id.desc())
            .limit(limit)
        )
    rows = db.execute(query).scalars().all()
    return [_prompt_trial_to_dict(row) for row in rows]


def get_prompt_trial(db: Session, trial_id: int) -> dict | None:
    row = db.get(PromptTrial, trial_id)
    return _prompt_trial_to_dict(row) if row is not None else None
