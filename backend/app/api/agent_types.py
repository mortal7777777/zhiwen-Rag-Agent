"""子代理类型注册表 API（Phase A 6.1）。

GET 列表 / POST 新建自定义类型 / PATCH 编辑 / POST reset 恢复内置默认。
所有写入立即刷新进程级注册表，下一个子任务即生效（进行中的子代理不受影响）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..agent.agent_types import (
    AGENT_MODES,
    ALL_SUBAGENT_TOOLS,
    DEFAULT_AGENT_TYPES,
    MODE_SHORT_LABELS,
    MODE_TOOLS,
    TOOL_LABELS,
    normalize_mode,
    refresh_registry,
)
from ..db import get_db
from ..db import repository as repo
from ..schemas import AgentTypeCreate, AgentTypeOut, AgentTypeUpdate

router = APIRouter(tags=["agent-types"])

_BUILTIN_KEYS = {item["key"] for item in DEFAULT_AGENT_TYPES}


def _to_out(spec: dict) -> dict:
    return {
        "key": spec["key"],
        "name": spec.get("name") or spec["key"],
        "description": spec.get("description") or "",
        "mode": spec.get("mode") or "research",
        "system_prompt": spec.get("system_prompt") or "",
        "tools": list(spec.get("tools") or []),
        "max_rounds": spec.get("max_rounds"),
        "model_provider_id": spec.get("model_provider_id") or "",
        "model": spec.get("model") or "",
        "thinking": spec.get("thinking") or "inherit",
        "llm_budget": spec.get("llm_budget"),
        "read_only": bool(spec.get("read_only", True)),
        "enabled": bool(spec.get("enabled", True)),
        "version": int(spec.get("version") or 1),
        "updated_at": spec.get("updated_at"),
        "is_builtin": spec["key"] in _BUILTIN_KEYS,
    }


def _validate(values: dict, *, current_mode: str | None = None) -> dict:
    """校验并收窄写入值；返回可直接落库的字段。"""
    out = dict(values)
    if "mode" in out:
        mode = str(out["mode"] or "").strip().lower()
        if mode not in AGENT_MODES:
            raise HTTPException(400, f"mode 必须是 {'/'.join(AGENT_MODES)}")
        out["mode"] = mode
    mode = out.get("mode") or current_mode or "research"
    if "thinking" in out:
        thinking = str(out["thinking"] or "inherit").strip().lower()
        if thinking not in ("inherit", "on", "off"):
            raise HTTPException(400, "thinking 必须是 inherit/on/off")
        out["thinking"] = thinking
    if "tools" in out:
        raw = out.get("tools") or []
        if not isinstance(raw, list):
            raise HTTPException(400, "tools 必须是字符串数组")
        tools = [str(t).strip() for t in raw if str(t).strip()]
        unknown = [t for t in tools if t not in ALL_SUBAGENT_TOOLS]
        if unknown:
            raise HTTPException(400, f"未知工具：{', '.join(unknown)}")
        allowed = set(MODE_TOOLS[normalize_mode(mode)])
        outside = [t for t in tools if t not in allowed]
        if outside:
            raise HTTPException(
                400,
                f"工具 {', '.join(outside)} 不属于 mode={mode} 的工具集（只允许收窄，不允许扩权）",
            )
        out["tools"] = list(dict.fromkeys(tools))
    if mode != "execute":
        # 非 execute 一律只读；execute 可以由调用方显式设为只读（收窄）
        out["read_only"] = True
    return out


@router.get("/agent-types/meta")
def agent_type_meta() -> dict:
    """前端编辑表单的元数据：每个 mode 可选的工具子集与 thinking 取值。"""
    return {
        "modes": {mode: list(MODE_TOOLS[mode]) for mode in AGENT_MODES},
        "thinking": ["inherit", "on", "off"],
        # 图形化编辑器的可读标签（工具多选/类型卡片）
        "tool_labels": {
            tool: TOOL_LABELS.get(tool, tool) for tool in ALL_SUBAGENT_TOOLS
        },
        "mode_labels": dict(MODE_SHORT_LABELS),
    }


@router.get("/agent-types", response_model=list[AgentTypeOut])
def list_agent_types(db: Session = Depends(get_db)) -> list[dict]:
    rows = repo.list_agent_types(db)
    if not rows:
        from ..agent.agent_types import list_agent_type_specs

        rows = list_agent_type_specs()
    return [_to_out(row) for row in rows]


@router.post("/agent-types", response_model=AgentTypeOut)
def create_agent_type(
    payload: AgentTypeCreate, db: Session = Depends(get_db)
) -> dict:
    key = payload.key.strip().lower()
    if repo.get_agent_type_row(db, key) is not None:
        raise HTTPException(409, f"类型 {key} 已存在")
    values = _validate(
        payload.model_dump(exclude_unset=True, exclude={"key"}), current_mode=None
    )
    row = repo.create_agent_type(db, {"key": key, **values})
    refresh_registry(db)
    return _to_out(repo.row_to_spec(row))


@router.patch("/agent-types/{key}", response_model=AgentTypeOut)
def update_agent_type(
    key: str, payload: AgentTypeUpdate, db: Session = Depends(get_db)
) -> dict:
    row = repo.get_agent_type_row(db, key)
    if row is None:
        raise HTTPException(404, f"未知代理类型：{key}")
    values = _validate(
        payload.model_dump(exclude_unset=True), current_mode=row.mode
    )
    if "mode" in values and "tools" not in values:
        # 只改 mode 时同步收窄已存工具，避免 DB 里留下不属于新 mode 的工具
        allowed = set(MODE_TOOLS[normalize_mode(values["mode"])])
        current_tools = repo.row_to_spec(row)["tools"]
        values["tools"] = [t for t in current_tools if t in allowed]
    row = repo.update_agent_type(db, row, values)
    refresh_registry(db)
    return _to_out(repo.row_to_spec(row))


@router.post("/agent-types/{key}/reset", response_model=AgentTypeOut)
def reset_agent_type(key: str, db: Session = Depends(get_db)) -> dict:
    key = key.strip().lower()
    row = repo.get_agent_type_row(db, key)
    if row is None:
        raise HTTPException(404, f"未知代理类型：{key}")
    if key not in _BUILTIN_KEYS:
        raise HTTPException(400, "只有内置类型可以恢复默认")
    row = repo.reset_agent_type(db, row)
    refresh_registry(db)
    return _to_out(repo.row_to_spec(row))


@router.delete("/agent-types/{key}")
def delete_agent_type(key: str, db: Session = Depends(get_db)) -> dict:
    key = key.strip().lower()
    row = repo.get_agent_type_row(db, key)
    if row is None:
        raise HTTPException(404, f"未知代理类型：{key}")
    if key in _BUILTIN_KEYS:
        raise HTTPException(400, "内置类型不能删除，可恢复默认或停用")
    repo.delete_agent_type(db, row)
    refresh_registry(db)
    return {"ok": True, "key": key}
