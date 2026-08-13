"""高级功能接口：MCP 管理、受控执行工具、项目记忆、技能沙箱。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..agent.langgraph_agent import LangGraphAgentService
from ..config import get_settings
from ..db import get_db
from ..db import repository as repo
from ..project_memory import export_project_memory, load_project_memory, project_memory_path
from ..runtime_config import effective, load_overrides
from .deps import get_agent_service

router = APIRouter(tags=["advanced"])


# ---------------- 人工确认（HITL）审批 ----------------


class PermissionResolveRequest(BaseModel):
    approve: bool = True
    reason: str = ""
    remember: bool = False        # 批准并永久记住（写入命令白名单）
    remember_session: bool = False  # 批准并在本会话内不再询问


@router.post("/agent/permission/{request_id}/resolve")
def resolve_permission(
    request_id: str,
    payload: PermissionResolveRequest,
    db: Session = Depends(get_db),
) -> dict:
    """用户批准/拒绝一次敏感操作（写文件 / 编辑 / 删除 / 执行命令）。"""
    from ..permissions import get_permission_manager
    from ..runtime_config import load_overrides, save_overrides, effective

    req = get_permission_manager().resolve(
        request_id,
        payload.approve,
        (payload.reason or "").strip()[:200],
        remember_forever=bool(payload.remember),
        remember_session=bool(payload.remember_session),
    )
    if req is None:
        raise HTTPException(status_code=404, detail="审批请求不存在或已处理")
    if payload.approve and payload.remember and req.tool in ("bash", "command_tool"):
        # 永久记住：把命令写入白名单（自动放行前缀），以后不再询问
        settings = get_settings()
        load_overrides(db)
        command = str((req.arguments or {}).get("command") or "").strip()
        if command:
            current = effective(settings, "command_allowlist") or ""
            prefixes = [p.strip() for p in current.split(",") if p.strip()]
            if command not in prefixes:
                prefixes.append(command)
            save_overrides(db, {"command_allowlist": ", ".join(prefixes)})
    return {"ok": True}


@router.get("/agent/permissions")
def list_pending_permissions() -> dict:
    """当前待人工确认的审批请求列表（调试/监控用）。"""
    from ..permissions import get_permission_manager

    mgr = get_permission_manager()
    return {"pending": mgr.pending()}


# ---------------- TodoWrite 任务清单 ----------------


class TodoUpdateRequest(BaseModel):
    items: list[dict] = Field(default_factory=list)


@router.get("/todos/{conversation_id}")
def get_todos(conversation_id: int, db: Session = Depends(get_db)) -> dict:
    """读取某会话的任务清单。"""
    from ..todos import load_todos

    return {"conversation_id": conversation_id, "todos": load_todos(db, conversation_id)}


@router.put("/todos/{conversation_id}")
def save_todos(
    conversation_id: int,
    payload: TodoUpdateRequest,
    db: Session = Depends(get_db),
) -> dict:
    """覆盖保存某会话的任务清单（前端勾选/编辑后调用）。"""
    from ..todos import save_todos as persist

    items = [
        {
            "id": str(i.get("id") or ""),
            "text": str(i.get("text") or "")[:200],
            "done": bool(i.get("done")),
        }
        for i in (payload.items or [])
        if str(i.get("text") or "").strip()
    ]
    persist(db, conversation_id, items)
    return {"ok": True, "todos": items}


# ---------------- MCP 服务器管理 ----------------


@router.get("/mcp")
def list_mcp_servers(db: Session = Depends(get_db)) -> dict:
    """MCP 服务器配置列表。"""
    raw = repo.get_meta(db, "mcp_servers")
    servers = json.loads(raw) if raw else []
    return {"servers": servers}


@router.put("/mcp")
def save_mcp_servers(
    payload: dict,
    db: Session = Depends(get_db),
    agent: LangGraphAgentService = Depends(get_agent_service),
) -> dict:
    """保存 MCP 服务器配置并重新加载连接。"""
    cleaned = []
    for s in payload.get("servers") or []:
        cleaned.append(
            {
                "id": str(s.get("id") or ""),
                "name": str(s.get("name") or "").strip()[:60],
                "type": s.get("type") if s.get("type") in ("stdio", "http") else "stdio",
                "command": str(s.get("command") or "").strip(),
                "args": [str(a) for a in (s.get("args") or [])],
                "url": str(s.get("url") or "").strip(),
                "enabled": bool(s.get("enabled", True)),
            }
        )
    repo.set_meta(db, "mcp_servers", json.dumps(cleaned, ensure_ascii=False))
    agent.refresh()
    return {"ok": True, "servers": cleaned}


@router.post("/mcp/test")
def test_mcp_server(payload: dict) -> dict:
    """一次性测试 MCP 服务器连接（不保存）。"""
    from ..mcp_manager import MCPServerSession

    session = MCPServerSession(payload)
    ok = session.start(timeout=15)
    result = {
        "ok": ok,
        "server": session.name,
        "tools": [t["name"] for t in session.tools],
        "error": session.error,
    }
    session.close()
    return result


# ---------------- 原生 checkpointer：时间旅行（快照审计） ----------------


@router.get("/conversations/{conversation_id}/timeline")
def conversation_timeline(
    conversation_id: int,
    agent: LangGraphAgentService = Depends(get_agent_service),
) -> dict:
    """列出某会话的原生 checkpoint 快照（时间旅行审计）。"""
    saver = getattr(agent, "checkpoint_saver", None)
    if saver is None:
        return {"items": [], "note": "原生 checkpointer 未启用"}
    items = []
    for tup in saver.list(
        {"configurable": {"thread_id": f"conv:{conversation_id}"}}
    ):
        cp = tup.checkpoint or {}
        ch = cp.get("channel_values") or {}
        msgs = ch.get("messages") or []
        items.append(
            {
                "checkpoint_id": cp.get("id"),
                "created_at": cp.get("ts"),
                "messages": len(msgs),
                "plan": ch.get("plan_steps") or [],
                "todos": ch.get("todos") or [],
                "plan_done_count": ch.get("plan_done_count", 0),
                "sources_count": len(ch.get("sources") or []),
            }
        )
    return {"items": list(reversed(items))[:50]}


@router.get("/conversations/{conversation_id}/timeline/{checkpoint_id}")
def conversation_timeline_detail(
    conversation_id: int,
    checkpoint_id: str,
    agent: LangGraphAgentService = Depends(get_agent_service),
) -> dict:
    """查看某个快照的完整内容（消息/计划/任务清单/来源/轨迹）。"""
    saver = getattr(agent, "checkpoint_saver", None)
    if saver is None:
        raise HTTPException(status_code=404, detail="原生 checkpointer 未启用")
    tup = saver.get_tuple(
        {
            "configurable": {
                "thread_id": f"conv:{conversation_id}",
                "checkpoint_id": checkpoint_id,
            }
        }
    )
    if tup is None:
        raise HTTPException(status_code=404, detail="快照不存在")
    ch = (tup.checkpoint or {}).get("channel_values") or {}
    return {
        "checkpoint_id": (tup.checkpoint or {}).get("id"),
        "created_at": (tup.checkpoint or {}).get("ts"),
        "messages": [
            {"type": type(m).__name__, "content": getattr(m, "content", "")}
            for m in (ch.get("messages") or [])
        ],
        "plan": ch.get("plan_steps") or [],
        "todos": ch.get("todos") or [],
        "sources": ch.get("sources") or [],
        "tool_trace": ch.get("tool_trace") or [],
    }


# ---------------- 生命周期 Hooks（PreToolUse / PostToolUse） ----------------


class HooksUpdateRequest(BaseModel):
    hooks: list[dict] = Field(default_factory=list)


@router.get("/hooks")
def list_hooks(db: Session = Depends(get_db)) -> dict:
    """读取 hooks 配置。"""
    from ..hooks import load_hooks

    return {"hooks": load_hooks(db)}


@router.put("/hooks")
def save_hooks(
    payload: HooksUpdateRequest,
    db: Session = Depends(get_db),
) -> dict:
    """保存 hooks 配置（立即生效，无需重启）。"""
    cleaned = []
    for h in payload.hooks:
        command = str(h.get("command") or "").strip()
        if not command:
            continue
        events = [
            e
            for e in (h.get("events") or [])
            if e in ("pre_tool_use", "post_tool_use")
        ]
        cleaned.append(
            {
                "name": str(h.get("name") or "").strip()[:60] or command[:40],
                "command": command[:300],
                "events": events,
                "timeout": max(1, min(60, int(h.get("timeout") or 15))),
            }
        )
    repo.set_meta(db, "hooks", json.dumps(cleaned, ensure_ascii=False))
    return {"ok": True, "hooks": cleaned}


# ---------------- 受控执行工具（设置页测试用） ----------------


class ToolExecRequest(BaseModel):
    type: str = Field(..., pattern="^(file|command)$")
    operation: str = "list"
    path: str = "."
    content: str = ""
    command: str = ""


@router.post("/tools/execute")
def execute_tool(
    req: ToolExecRequest,
    db: Session = Depends(get_db),
) -> dict:
    """执行受控工具（文件操作 / 白名单命令）。"""
    settings = get_settings()
    load_overrides(db)
    if not effective(settings, "advanced_tools_enabled", False):
        return {
            "error": "受控执行工具未开启",
            "summary": "请先在 设置 -> 工具 中开启并配置白名单",
        }
    from ..tools_extra import make_command_tool, make_file_tool

    if req.type == "file":
        return make_file_tool(settings).invoke(
            {"operation": req.operation, "path": req.path, "content": req.content}
        )
    if not req.command.strip():
        return {"error": "命令为空", "summary": "请输入要执行的命令"}
    return make_command_tool(settings).invoke({"command": req.command})


# ---------------- 项目记忆（AGENTS.md） ----------------


@router.get("/project-memory")
def read_project_memory(db: Session = Depends(get_db)) -> dict:
    """读取文件型项目记忆。"""
    settings = get_settings()
    return {
        "path": str(project_memory_path(settings)),
        "content": load_project_memory(settings) or "",
    }


@router.post("/project-memory/export")
def export_memory(db: Session = Depends(get_db)) -> dict:
    """把画像摘要与长期记忆导出为 AGENTS.md。"""
    return export_project_memory(get_settings(), db)


# ---------------- 技能沙箱 ----------------


@router.get("/skills/{name}/commands")
def skill_commands(name: str) -> dict:
    """提取技能 SKILL.md 中的可执行命令（供预览/确认）。"""
    from ..skills import scan_skills

    for skill in scan_skills():
        if skill["name"] == name:
            try:
                text = open(skill["path"], encoding="utf-8", errors="ignore").read()
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"读取技能失败：{exc}")
            from ..tools_extra import extract_commands_from_skill

            return {"name": name, "commands": extract_commands_from_skill(text)}
    raise HTTPException(status_code=404, detail="技能不存在")


@router.post("/skills/{name}/run")
def run_skill_command(
    name: str,
    payload: dict,
    db: Session = Depends(get_db),
) -> dict:
    """在沙箱中执行技能命令（需开启技能沙箱 + 命令在白名单内）。"""
    settings = get_settings()
    load_overrides(db)
    if not effective(settings, "skill_sandbox_enabled", False):
        return {"ok": False, "error": "技能沙箱未开启（设置 -> 工具）"}
    command = (payload.get("command") or "").strip()
    if not command:
        return {"ok": False, "error": "命令为空"}
    from ..tools_extra import run_sandbox_command

    return run_sandbox_command(settings, command)
