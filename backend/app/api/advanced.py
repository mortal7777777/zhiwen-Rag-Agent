"""高级功能接口：MCP 管理、受控执行工具、项目记忆、技能沙箱。"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..agent.langgraph_agent import LangGraphAgentService
from ..config import get_settings
from ..db import get_db
from ..db import repository as repo
from ..mcp_secrets import mask_mcp_servers, merge_mcp_secrets
from ..project_memory import export_project_memory, load_project_memory, project_memory_path
from ..runtime_config import effective_or, load_overrides
from .deps import get_agent_service, get_service

router = APIRouter(tags=["advanced"])


@router.get("/models/status")
def models_status(service=Depends(get_service)) -> dict:
    """本地 GPU 模型加载/空闲状态（不触发模型加载）。"""
    return service.gpu_models_status()


@router.post("/models/warm")
def models_warm(service=Depends(get_service)) -> dict:
    """立即加载本地 BGE/reranker（空闲释放后的手动预热）。"""
    service.acquire_gpu()
    try:
        _ = service.embeddings
        _ = service.reranker
        return service.gpu_models_status()
    finally:
        service.release_gpu()


# 人工确认（HITL）审批接口已移除：审批走原生 interrupt 挂起，
# 决定经 POST /api/agent/resume 提交（见 api/agent.py），不再有 per-id 端点。


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
def list_mcp_servers(
    db: Session = Depends(get_db),
    agent: LangGraphAgentService = Depends(get_agent_service),
) -> dict:
    """MCP 服务器配置列表 + 各服务器运行状态。"""
    raw = repo.get_meta(db, "mcp_servers")
    servers = json.loads(raw) if raw else []
    return {"servers": mask_mcp_servers(servers), "status": agent.mcp_status()}


@router.put("/mcp")
def save_mcp_servers(
    payload: dict,
    request: Request = None,
    db: Session = Depends(get_db),
    agent: LangGraphAgentService = Depends(get_agent_service),
) -> dict:
    """保存 MCP 服务器配置并重新加载连接。"""
    from .fs import require_local_or_token

    require_local_or_token(request)
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
                "env": {
                    str(k): str(v)
                    for k, v in (s.get("env") or {}).items()
                    if str(k).strip()
                },
                "headers": {
                    str(k): str(v)
                    for k, v in (s.get("headers") or {}).items()
                    if str(k).strip()
                },
            }
        )
    raw_old = repo.get_meta(db, "mcp_servers")
    existing = json.loads(raw_old) if raw_old else []
    cleaned = merge_mcp_secrets(cleaned, existing)
    repo.set_meta(db, "mcp_servers", json.dumps(cleaned, ensure_ascii=False))
    agent.refresh()
    # 保存即应用：为启用项立即发起连接、关闭禁用项；状态回传面板
    connect = agent.connect_mcp_servers(db)
    return {
        "ok": True,
        "servers": mask_mcp_servers(cleaned),
        "status": connect.get("status", {}),
    }


@router.post("/mcp/test")
def test_mcp_server(payload: dict, request: Request = None) -> dict:
    """一次性测试 MCP 服务器连接（不保存、独立进程）。"""
    from ..mcp_manager import MCPServerSession
    from .fs import require_local_or_token

    require_local_or_token(request)
    session = MCPServerSession(payload)
    started = time.time()
    ok = session.start(timeout=60)
    return {
        "ok": ok,
        "server": session.name,
        "tools": [t["name"] for t in (session.tools or session.available_tools())],
        "error": session.error,
        "latency_ms": int((time.time() - started) * 1000),
    }
    # 说明：session 在测试进程内独立启动，结果不进入运行时管理器；
    # 面板状态以 GET /mcp 的 status 为准（随 Agent 运行/重连更新）。


@router.post("/mcp/connect")
def connect_mcp(
    payload: dict | None = None,
    db: Session = Depends(get_db),
    agent: LangGraphAgentService = Depends(get_agent_service),
) -> dict:
    """立即启动/重连 MCP 服务器（面板「启动」按钮；空 ids = 全部启用项）。

    连接在后台进行、不阻塞请求；返回后轮询 GET /mcp 的 status 查看结果。
    """
    raw_ids = (payload or {}).get("ids") or []
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=422, detail="ids 必须为字符串数组")
    ids = [str(i) for i in raw_ids] or None
    return agent.connect_mcp_servers(db, ids)


# ---------------- 原生 checkpointer：时间旅行（快照审计） ----------------


def _checkpoint_full_messages(
    ch: dict, db: Session, conversation_id: int
) -> list:
    """把检查点 state 消息与历史前缀拼回完整链（旧格式原样返回）。

    P2-8 后 state["messages"] 不再含历史窗口；时间线展示/回滚需要按
    history_row_ids 从 DB 取回当时的历史，否则回滚会丢掉更早的对话。
    """
    messages = list(ch.get("messages") or [])
    insert_at = ch.get("history_insert_at")
    row_ids = ch.get("history_row_ids") or []
    if insert_at is None or not row_ids:
        return messages
    try:
        rows = repo.get_messages_by_ids(db, [int(i) for i in row_ids])
        if not rows:
            return messages
        from ..agent.utils import _rows_to_history

        history = _rows_to_history(rows, [])
        return (
            messages[: int(insert_at)] + history + messages[int(insert_at):]
        )
    except Exception:
        return messages


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
        # 新格式检查点不含历史：展示总条数时把窗口行数补回（近似值）
        history_count = (
            len(ch.get("history_row_ids") or [])
            if ch.get("history_insert_at") is not None
            else 0
        )
        items.append(
            {
                "checkpoint_id": cp.get("id"),
                "created_at": cp.get("ts"),
                "messages": len(msgs) + history_count,
                "plan": ch.get("plan_steps") or [],
                "todos": ch.get("todos") or [],
                "plan_done_count": ch.get("plan_done_count", 0),
                "sources_count": len(ch.get("sources") or []),
            }
        )
    # 最新在前（checkpoint_id 时间有序，升序排布对 saver.list 的返回顺序不敏感）；
    # 只取最近 50 条——旧实现 reversed 后 [:50] 实际留下的是最旧的 50 条
    items.sort(key=lambda it: it.get("checkpoint_id") or "", reverse=True)
    return {"items": items[:50]}


@router.get("/conversations/{conversation_id}/timeline/{checkpoint_id}")
def conversation_timeline_detail(
    conversation_id: int,
    checkpoint_id: str,
    agent: LangGraphAgentService = Depends(get_agent_service),
    db: Session = Depends(get_db),
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
    full = _checkpoint_full_messages(ch, db, conversation_id)
    return {
        "checkpoint_id": (tup.checkpoint or {}).get("id"),
        "created_at": (tup.checkpoint or {}).get("ts"),
        "messages": [
            {"type": type(m).__name__, "content": getattr(m, "content", "")}
            for m in full
        ],
        "plan": ch.get("plan_steps") or [],
        "todos": ch.get("todos") or [],
        "sources": ch.get("sources") or [],
        "tool_trace": ch.get("tool_trace") or [],
    }


@router.post("/conversations/{conversation_id}/timeline/{checkpoint_id}/rollback")
def rollback_to_checkpoint(
    conversation_id: int,
    checkpoint_id: str,
    db: Session = Depends(get_db),
    agent: LangGraphAgentService = Depends(get_agent_service),
) -> dict:
    """把会话回滚到某个快照：重建消息历史与任务清单，下一次提问从该状态继续。"""
    from ..native_checkpoint import messages_to_history_rows
    from ..todos import save_todos
    from .agent import is_conversation_active

    if is_conversation_active(conversation_id):
        raise HTTPException(status_code=409, detail="会话正在运行，先停止后再回滚")
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
    rows = messages_to_history_rows(
        _checkpoint_full_messages(ch, db, conversation_id)
    )
    repo.clear_messages(db, conversation_id)
    for row in rows:
        repo.add_message(
            db,
            conversation_id,
            row["role"],
            row["content"],
            tool_trace=row.get("tool_trace"),
        )
    save_todos(db, conversation_id, ch.get("todos") or [])
    # 线程里仍留着回滚点之后的快照；删除线程强制下一轮从 MySQL 历史重建，
    # 避免崩溃续跑/恢复读到与已回滚消息矛盾的状态。
    abandon = getattr(agent, "abandon_run", None)
    if callable(abandon):
        try:
            abandon(conversation_id)
        except Exception:
            pass
    return {
        "ok": True,
        "checkpoint_id": checkpoint_id,
        "restored_messages": len(rows),
        "restored_todos": len(ch.get("todos") or []),
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
    request: Request = None,
    db: Session = Depends(get_db),
) -> dict:
    """保存 hooks 配置（立即生效，无需重启）。"""
    from .fs import require_local_or_token

    require_local_or_token(request)
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
    request: Request = None,
    db: Session = Depends(get_db),
) -> dict:
    """执行受控工具（文件操作 / 白名单命令）。"""
    from .fs import require_local_or_token

    require_local_or_token(request)
    settings = get_settings()
    load_overrides(db)
    # 注意：这里原来写的是 effective(..., False)，会让 config/env 里的 True 失效，
    # 导致"设置页没保存过就报未开启"。统一走 effective_or，与 Agent 侧同语义。
    if not effective_or(settings, "advanced_tools_enabled", True):
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
    request: Request = None,
    db: Session = Depends(get_db),
) -> dict:
    """在沙箱中执行技能命令（需开启技能沙箱 + 命令在白名单内）。"""
    from .fs import require_local_or_token

    require_local_or_token(request)
    settings = get_settings()
    load_overrides(db)
    if not effective_or(settings, "skill_sandbox_enabled", False):
        return {"ok": False, "error": "技能沙箱未开启（设置 -> 工具）"}
    command = (payload.get("command") or "").strip()
    if not command:
        return {"ok": False, "error": "命令为空"}
    from ..tools_extra import run_sandbox_command

    return run_sandbox_command(settings, command)
