"""子代理类型注册表数据访问（2026-11-21，Phase A 6.1）。"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgentType
from .common import _json_loads

logger = logging.getLogger(__name__)

_EDITABLE_FIELDS = (
    "name",
    "description",
    "mode",
    "system_prompt",
    "tools",
    "max_rounds",
    "model_provider_id",
    "model",
    "thinking",
    "llm_budget",
    "read_only",
    "enabled",
)


def row_to_spec(row: AgentType) -> dict:
    tools = _json_loads(row.tools) if isinstance(row.tools, str) else row.tools
    if not isinstance(tools, list):
        tools = []
    return {
        "key": row.key,
        "name": row.name,
        "description": row.description or "",
        "mode": row.mode,
        "system_prompt": row.system_prompt or "",
        "tools": [str(t) for t in tools],
        "max_rounds": row.max_rounds,
        "model_provider_id": row.model_provider_id or "",
        "model": row.model or "",
        "thinking": row.thinking or "inherit",
        "llm_budget": row.llm_budget,
        "read_only": bool(row.read_only),
        "enabled": bool(row.enabled),
        "version": int(row.version or 1),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_agent_types(db: Session) -> list[dict]:
    rows = db.execute(select(AgentType).order_by(AgentType.id)).scalars().all()
    return [row_to_spec(row) for row in rows]


def get_agent_type_row(db: Session, key: str) -> AgentType | None:
    return (
        db.execute(select(AgentType).where(AgentType.key == str(key).strip().lower()))
        .scalars()
        .first()
    )


def seed_agent_types(db: Session) -> None:
    """幂等写入四种内置类型；已存在的行不覆盖用户编辑。"""
    from ...agent.agent_types import DEFAULT_AGENT_TYPES

    changed = False
    for preset in DEFAULT_AGENT_TYPES:
        try:
            row = get_agent_type_row(db, preset["key"])
            if row is not None:
                continue
            db.add(
                AgentType(
                    key=preset["key"],
                    name=preset["name"],
                    description=preset["description"],
                    mode=preset["mode"],
                    system_prompt="",
                    tools=json.dumps([], ensure_ascii=False),
                    max_rounds=None,
                    model_provider_id="",
                    model="",
                    thinking="inherit",
                    llm_budget=None,
                    read_only=bool(preset.get("read_only", True)),
                    enabled=True,
                    version=1,
                )
            )
            changed = True
        except Exception as exc:  # noqa: BLE001 - 单个种子失败不阻断启动
            db.rollback()
            logger.warning("种子代理类型 %s 失败：%s", preset.get("key"), exc)
            changed = False
    if changed:
        db.commit()


def create_agent_type(db: Session, values: dict) -> AgentType:
    row = AgentType(
        key=str(values["key"]).strip().lower(),
        name=str(values.get("name") or values["key"]).strip()[:100],
        description=str(values.get("description") or "")[:255],
        mode=str(values.get("mode") or "research"),
        system_prompt=str(values.get("system_prompt") or ""),
        tools=json.dumps(list(values.get("tools") or []), ensure_ascii=False),
        max_rounds=values.get("max_rounds"),
        model_provider_id=str(values.get("model_provider_id") or ""),
        model=str(values.get("model") or ""),
        thinking=str(values.get("thinking") or "inherit"),
        llm_budget=values.get("llm_budget"),
        read_only=bool(values.get("read_only", True)),
        enabled=bool(values.get("enabled", True)),
        version=1,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _apply_values(row: AgentType, values: dict) -> None:
    for field in _EDITABLE_FIELDS:
        if field not in values:
            continue
        value = values[field]
        if field == "tools":
            row.tools = json.dumps(list(value or []), ensure_ascii=False)
        elif field in ("max_rounds", "llm_budget"):
            setattr(row, field, value)
        elif field == "read_only":
            row.read_only = bool(value)
        elif field == "enabled":
            row.enabled = bool(value)
        else:
            setattr(row, field, value)


def update_agent_type(db: Session, row: AgentType, values: dict) -> AgentType:
    """按提交字段更新并递增 version（无实际变化不增版本）。"""
    before = row_to_spec(row)
    _apply_values(row, values)
    after = row_to_spec(row)
    changed = any(before.get(k) != after.get(k) for k in _EDITABLE_FIELDS if k in values)
    if changed:
        row.version = int(row.version or 1) + 1
    db.commit()
    db.refresh(row)
    return row


def reset_agent_type(db: Session, row: AgentType) -> AgentType:
    from ...agent.agent_types import default_type

    preset = default_type(row.key)
    if preset is None:
        raise KeyError(row.key)
    _apply_values(
        row,
        {
            "name": preset["name"],
            "description": preset["description"],
            "mode": preset["mode"],
            "system_prompt": preset["system_prompt"],
            "tools": preset["tools"],
            "max_rounds": preset["max_rounds"],
            "model_provider_id": preset["model_provider_id"],
            "model": preset["model"],
            "thinking": preset["thinking"],
            "llm_budget": preset["llm_budget"],
            "read_only": preset["read_only"],
            "enabled": preset["enabled"],
        },
    )
    row.version = int(row.version or 1) + 1
    db.commit()
    db.refresh(row)
    return row


def delete_agent_type(db: Session, row: AgentType) -> None:
    db.delete(row)
    db.commit()
