"""会话级系统提示词解析与锁定。

设计要点：
- 版本是元数据，绝不写进 SystemMessage：消息链字节只由片段内容决定；
- 会话首次运行锁定 bundle（模板版本 + 各片段版本），编辑只影响新会话；
- 解析失败/DB 不可用回退代码默认，不阻断对话；
- `PROMPT_VERSIONING_ENABLED=0` 时走旧的“每轮现读模板”语义，作为紧急回滚。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .agent.prompts import (
    DEFAULT_SYSTEM_PROMPT,
    FRAGMENTS_BY_KEY,
    default_fragments,
)
from .db import repository as repo
from .db.models import Conversation
from .runtime_config import effective_or

logger = logging.getLogger(__name__)


@dataclass
class PromptResolution:
    template_content: str
    fragments: dict[str, str]
    telemetry: dict
    lock_written: bool = False
    source: str = "locked"


def _bundle_hash(bundle: dict) -> str:
    payload = {k: v for k, v in bundle.items() if k != "bundle_hash"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _versioning_enabled(settings) -> bool:
    if settings is None:
        return True
    return bool(effective_or(settings, "prompt_versioning_enabled", True))


def _fallback_template(db: Session | None, template_id: int | None):
    """按绑定 id 取模板；为空/不存在时退回隐藏 default；再失败返回 None。"""
    if db is None:
        return None
    if template_id:
        tpl = repo.get_template(db, template_id)
        if tpl is not None:
            return tpl
    return repo.get_template_by_system_key(db, "default")


def _template_for_conversation(db: Session | None, conv: Conversation | None):
    if db is None:
        return None
    return _fallback_template(db, conv.template_id if conv else None)


def _build_bundle(db: Session, tpl) -> dict:
    versions = repo.list_template_versions(db, tpl.id)
    if not versions:
        # 理论上 seed/create 已建 v1；兜底补一版，避免锁到空版本
        repo.publish_template_content(db, tpl, tpl.content, note="锁定前补版本")
        versions = repo.list_template_versions(db, tpl.id)
    latest = versions[0]
    fragments: dict[str, dict] = {}
    for fragment in repo.list_prompt_fragments(db):
        fragments[fragment["key"]] = {
            "version_id": fragment["version_id"],
            "version_no": fragment["version_no"],
            "hash": fragment["content_hash"],
        }
    bundle = {
        "template_id": tpl.id,
        "template_key": tpl.system_key or "",
        "template_name": tpl.name,
        "template_category": tpl.category,
        "template_version_id": latest["id"],
        "template_version_no": latest["version_no"],
        "template_content_hash": latest["content_hash"],
        "fragments": fragments,
    }
    bundle["bundle_hash"] = _bundle_hash(bundle)
    return bundle


def _content_from_bundle(
    db: Session, bundle: dict, tpl
) -> tuple[str, dict[str, str], bool]:
    """按锁定的版本指针取内容；版本缺失回退当前模板/片段，返回 fallback 标记。"""
    fallback = False
    template_content = tpl.content if tpl is not None else DEFAULT_SYSTEM_PROMPT
    version = repo.get_template_version(
        db, tpl.id if tpl is not None else 0,
        int(bundle.get("template_version_id") or 0),
    )
    if version is not None:
        template_content = version.content
    else:
        fallback = True

    fragments = default_fragments()
    for key, ref in (bundle.get("fragments") or {}).items():
        version_id = int((ref or {}).get("version_id") or 0)
        frag_version = (
            repo.get_prompt_fragment_version(db, key, version_id)
            if version_id
            else None
        )
        if frag_version is not None:
            fragments[key] = frag_version.content
            continue
        fallback = True
        fragment = repo.get_prompt_fragment(db, key)
        if fragment is not None:
            fragments[key] = fragment.content
    return template_content, fragments, fallback


def _telemetry(
    source: str,
    bundle: dict | None,
    *,
    template_content: str,
    fragments: dict[str, str],
    override: bool,
    fallback: bool = False,
    locked_at=None,
) -> dict:
    bundle = bundle or {}
    return {
        "source": source,
        "bundle_hash": bundle.get("bundle_hash"),
        "template_id": bundle.get("template_id"),
        "template_key": bundle.get("template_key"),
        "template_name": bundle.get("template_name"),
        "template_category": bundle.get("template_category"),
        "template_version_id": bundle.get("template_version_id"),
        "template_version_no": bundle.get("template_version_no"),
        "template_hash": bundle.get("template_content_hash"),
        "system_prompt_override": bool(override),
        "fragments": bundle.get("fragments") or {},
        "fallback": bool(fallback),
        "template_chars": len(template_content or ""),
        "fragment_chars": sum(len(v or "") for v in fragments.values()),
        "locked_at": locked_at.isoformat(timespec="seconds") if locked_at else None,
    }


def resolve_conversation_prompt(
    db: Session | None,
    conversation_id: int | None,
    request_template_id: int | None,
    request_system_prompt: str | None,
    settings=None,
) -> PromptResolution:
    """解析本轮系统提示词内容（模板 + 片段）与会话锁。

    优先级：请求 system_prompt 覆盖内容；模板选择跟随会话（显式换模板
    由调用方先写入 conv.template_id）；版本始终优先用会话锁。
    """
    override = bool(request_system_prompt and request_system_prompt.strip())
    if db is None:
        return PromptResolution(
            template_content=request_system_prompt or DEFAULT_SYSTEM_PROMPT,
            fragments=default_fragments(),
            telemetry=_telemetry(
                "fallback",
                None,
                template_content=request_system_prompt or DEFAULT_SYSTEM_PROMPT,
                fragments=default_fragments(),
                override=override,
                fallback=True,
            ),
            source="fallback",
        )

    try:
        conv = (
            repo.get_conversation(db, conversation_id) if conversation_id else None
        )
        if not _versioning_enabled(settings):
            tpl = repo.get_template(db, request_template_id) if request_template_id else None
            content = request_system_prompt or (
                tpl.content if tpl is not None else DEFAULT_SYSTEM_PROMPT
            )
            fragments = default_fragments()
            return PromptResolution(
                template_content=content,
                fragments=fragments,
                telemetry=_telemetry(
                    "disabled",
                    None,
                    template_content=content,
                    fragments=fragments,
                    override=override,
                ),
                source="disabled",
            )

        # 请求显式换模板时，先把绑定写入会话（会清空旧锁）。
        if (
            conv is not None
            and request_template_id is not None
            and (conv.template_id or None) != (request_template_id or None)
        ):
            conv = repo.update_conversation(
                db, conv.id, template_id=request_template_id
            )

        tpl = _template_for_conversation(db, conv)
        lock = repo.get_conversation_prompt_lock(conv)
        lock_written = False
        fallback = False

        if lock is not None and (tpl is None or lock.get("template_id") == tpl.id):
            template_content, fragments, fallback = _content_from_bundle(
                db, lock, tpl
            )
            source = "locked"
        elif tpl is not None:
            lock = _build_bundle(db, tpl)
            if conv is not None:
                repo.set_conversation_prompt_lock(db, conv, lock)
                lock_written = True
            template_content = tpl.content
            fragments = {
                f["key"]: f["content"] for f in repo.list_prompt_fragments(db)
            }
            source = "new_lock"
        else:
            template_content = DEFAULT_SYSTEM_PROMPT
            fragments = default_fragments()
            fallback = True
            source = "fallback"

        if override:
            template_content = str(request_system_prompt)
            source = "override"

        telemetry = _telemetry(
            source,
            lock,
            template_content=template_content,
            fragments=fragments,
            override=override,
            fallback=fallback,
            locked_at=conv.prompt_locked_at if conv is not None else None,
        )
        return PromptResolution(
            template_content=template_content,
            fragments=fragments,
            telemetry=telemetry,
            lock_written=lock_written,
            source=source,
        )
    except Exception as exc:
        logger.warning("提示词版本解析失败，回退代码默认：%s", exc)
        content = request_system_prompt or DEFAULT_SYSTEM_PROMPT
        fragments = default_fragments()
        return PromptResolution(
            template_content=content,
            fragments=fragments,
            telemetry=_telemetry(
                "fallback",
                None,
                template_content=content,
                fragments=fragments,
                override=override,
                fallback=True,
            ),
            source="fallback",
        )


def apply_latest(db: Session, conversation_id: int) -> dict:
    """把会话锁升级到当前模板/片段的最新版本（显式断一次前缀缓存）。"""
    conv = repo.get_conversation(db, conversation_id)
    if conv is None:
        return {"ok": False, "error": "会话不存在"}
    tpl = _template_for_conversation(db, conv)
    if tpl is None:
        return {"ok": False, "error": "没有可用的提示词模板"}
    old_hash = conv.prompt_bundle_hash
    old_bundle = repo.get_conversation_prompt_lock(conv) or {}
    new_bundle = _build_bundle(db, tpl)
    repo.set_conversation_prompt_lock(db, conv, new_bundle)
    return {
        "ok": True,
        # 首次锁定不改变下一轮内容（本来就按最新渲染），不算断缓存
        "cache_break": bool(old_hash) and old_hash != new_bundle.get("bundle_hash"),
        "first_lock": old_hash is None,
        "old_bundle_hash": old_hash,
        "bundle": new_bundle,
        "old_bundle": old_bundle,
    }


def pin_conversation_prompt(
    db: Session,
    conversation_id: int,
    template_version_id: int,
    fragment_version_ids: dict[str, int] | None = None,
) -> dict:
    """把会话锁定到指定模板版本（片段默认取各自最新版本）。"""
    conv = repo.get_conversation(db, conversation_id)
    if conv is None:
        return {"ok": False, "error": "会话不存在"}
    tpl = _template_for_conversation(db, conv)
    if tpl is None:
        return {"ok": False, "error": "没有可用的提示词模板"}
    version = repo.get_template_version(db, tpl.id, template_version_id)
    if version is None:
        return {"ok": False, "error": "模板版本不存在"}

    fragments: dict[str, dict] = {}
    for item in repo.list_prompt_fragments(db):
        key = item["key"]
        version_id = (fragment_version_ids or {}).get(key) or item.get("version_id")
        fragment_version = (
            repo.get_prompt_fragment_version(db, key, int(version_id))
            if version_id
            else None
        )
        if fragment_version is None:
            return {"ok": False, "error": f"片段 {key} 没有可锁定的版本"}
        fragments[key] = {
            "version_id": fragment_version.id,
            "version_no": fragment_version.version_no,
            "hash": fragment_version.content_hash,
        }
    bundle = {
        "template_id": tpl.id,
        "template_key": tpl.system_key or "",
        "template_name": tpl.name,
        "template_category": tpl.category,
        "template_version_id": version.id,
        "template_version_no": version.version_no,
        "template_content_hash": version.content_hash,
        "fragments": fragments,
    }
    bundle["bundle_hash"] = _bundle_hash(bundle)
    repo.set_conversation_prompt_lock(db, conv, bundle)
    return {"ok": True, "bundle": bundle}


def conversation_prompt_status(db: Session | None, conversation_id: int) -> dict:
    if db is None:
        return {"enabled": False, "locked": None, "latest": None, "upgradable": False}
    conv = repo.get_conversation(db, conversation_id)
    if conv is None:
        return {"enabled": True, "locked": None, "latest": None, "upgradable": False}
    tpl = _template_for_conversation(db, conv)
    locked = repo.get_conversation_prompt_lock(conv)
    latest = _build_bundle(db, tpl) if tpl is not None else None
    outdated: list[str] = []
    outdated_details: list[dict] = []
    if locked is not None and latest is not None:
        if (locked.get("template_version_id") != latest.get("template_version_id")):
            outdated.append("template")
            outdated_details.append(
                {
                    "key": "template",
                    "title": "角色模板",
                    "name": latest.get("template_name")
                    or latest.get("template_key")
                    or "",
                    "locked_version_no": locked.get("template_version_no"),
                    "latest_version_no": latest.get("template_version_no"),
                }
            )
        locked_fragments = locked.get("fragments") or {}
        latest_fragments = latest.get("fragments") or {}
        for key, ref in latest_fragments.items():
            if (locked_fragments.get(key) or {}).get("version_id") != ref.get(
                "version_id"
            ):
                outdated.append(key)
                spec = FRAGMENTS_BY_KEY.get(key) or {}
                outdated_details.append(
                    {
                        "key": key,
                        "title": spec.get("title") or key,
                        "name": "",
                        "locked_version_no": (
                            locked_fragments.get(key) or {}
                        ).get("version_no"),
                        "latest_version_no": ref.get("version_no"),
                    }
                )
    return {
        "enabled": True,
        "locked": locked,
        "latest": latest,
        "upgradable": bool(locked is None or outdated),
        "outdated_units": outdated,
        "outdated_details": outdated_details,
    }


def preview_prompt_resolution(
    db: Session | None,
    conversation_id: int | None = None,
    template_id: int | None = None,
    template_content: str | None = None,
    fragment_key: str | None = None,
    fragment_content: str | None = None,
) -> dict:
    """只读预览：不写锁、不补版本，返回模板/片段内容与来源。

    模板选择优先级：显式 template_content > 显式 template_id > 会话锁定 >
    会话绑定模板 > 隐藏默认模板。片段优先使用会话锁定版本，未锁定用当前版本；
    可对单个片段做未保存内容覆盖。
    """
    if db is None:
        fragments = default_fragments()
        if fragment_key in FRAGMENTS_BY_KEY:
            fragments[fragment_key] = str(fragment_content or "")
        return {
            "source": "fallback",
            "locked": False,
            "fallback": True,
            "template": {
                "id": None,
                "name": "默认助手（内置）",
                "key": "default",
                "category": "general",
                "version_no": None,
                "content": str(template_content or DEFAULT_SYSTEM_PROMPT),
            },
            "fragments": [
                {
                    "key": key,
                    "title": spec["title"],
                    "scope": spec["scope"],
                    "content": fragments.get(key, spec["default"]),
                    "source": "override" if key == fragment_key else "fallback",
                    "version_no": None,
                }
                for key, spec in FRAGMENTS_BY_KEY.items()
            ],
        }

    conv = (
        repo.get_conversation(db, conversation_id) if conversation_id else None
    )
    tpl = repo.get_template(db, template_id) if template_id else None
    if tpl is None:
        tpl = _template_for_conversation(db, conv)

    lock = repo.get_conversation_prompt_lock(conv)
    lock_usable = bool(
        lock is not None and tpl is not None and lock.get("template_id") == tpl.id
    )
    fallback = False
    template_override = bool(template_content and template_content.strip())

    if template_override:
        content = str(template_content)
        source = "override"
        version_no = None
        current_fragments = repo.list_prompt_fragments(db)
        fragments = {f["key"]: f["content"] for f in current_fragments}
        fragment_versions = {f["key"]: f.get("version_no") for f in current_fragments}
    elif lock_usable:
        content, fragments, fallback = _content_from_bundle(db, lock, tpl)
        source = "locked"
        version_no = lock.get("template_version_no")
        fragment_versions = {
            key: (ref or {}).get("version_no")
            for key, ref in (lock.get("fragments") or {}).items()
        }
    else:
        if tpl is not None:
            versions = repo.list_template_versions(db, tpl.id)
            content = versions[0]["content"] if versions else tpl.content
            version_no = versions[0]["version_no"] if versions else None
        else:
            content = DEFAULT_SYSTEM_PROMPT
            version_no = None
            fallback = True
        current_fragments = repo.list_prompt_fragments(db)
        fragments = {f["key"]: f["content"] for f in current_fragments}
        fragment_versions = {f["key"]: f.get("version_no") for f in current_fragments}
        source = "latest"

    overridden = fragment_key in FRAGMENTS_BY_KEY
    if overridden:
        fragments = dict(fragments)
        fragments[fragment_key] = str(fragment_content or "")

    return {
        "source": source,
        "locked": lock is not None,
        "fallback": fallback,
        "template": {
            "id": tpl.id if tpl is not None else None,
            "name": (
                tpl.name
                if tpl is not None
                else "默认助手（内置）"
            ),
            "key": (tpl.system_key if tpl is not None else "default") or "",
            "category": (tpl.category if tpl is not None else "general") or "",
            "version_no": version_no,
            "content": content,
        },
        "fragments": [
            {
                "key": key,
                "title": spec["title"],
                "scope": spec["scope"],
                "content": fragments.get(key, spec["default"]),
                "source": (
                    "override"
                    if overridden and key == fragment_key
                    else (
                        "locked"
                        if lock_usable
                        and key in ((lock or {}).get("fragments") or {})
                        else "latest"
                    )
                ),
                "version_no": (
                    None
                    if overridden and key == fragment_key
                    else fragment_versions.get(key)
                ),
            }
            for key, spec in FRAGMENTS_BY_KEY.items()
        ],
    }
