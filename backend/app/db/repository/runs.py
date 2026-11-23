"""Agent 运行记录数据访问。"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AgentRun
from ..sanitize import sanitize_sources, sanitize_token_usage, truncate_tool_trace
from .common import _json_dumps, _json_loads

logger = logging.getLogger(__name__)


def create_agent_run(
    db: Session,
    *,
    conversation_id: int | None,
    question: str,
    plan: list | None,
    tool_trace: list | None,
    answer_len: int,
    latency_ms: int,
    status: str,
    error: str | None,
    token_usage: dict | None,
) -> AgentRun:
    run = AgentRun(
        conversation_id=conversation_id,
        question=question[:4000],
        plan=_json_dumps(plan),
        tool_trace=_json_dumps(truncate_tool_trace(tool_trace)),
        answer_len=answer_len,
        latency_ms=latency_ms,
        status=status,
        error=(error or "")[:4000] or None,
        token_usage=_json_dumps(sanitize_token_usage(token_usage)),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _agent_run_row(r: AgentRun) -> dict:
    return {
        "id": r.id,
        "conversation_id": r.conversation_id,
        "question": r.question,
        "plan": _json_loads(r.plan),
        "tool_trace": _json_loads(r.tool_trace),
        "answer_len": r.answer_len,
        "latency_ms": r.latency_ms,
        "status": r.status,
        "error": r.error,
        "token_usage": _json_loads(r.token_usage),
        "created_at": r.created_at,
    }


def list_agent_runs(
    db: Session,
    limit: int = 50,
    conversation_id: int | None = None,
) -> list[dict]:
    query = select(AgentRun).order_by(AgentRun.id.desc()).limit(limit)
    if conversation_id is not None:
        query = (
            select(AgentRun)
            .where(AgentRun.conversation_id == conversation_id)
            .order_by(AgentRun.id.desc())
            .limit(limit)
        )
    rows = db.execute(query).scalars().all()
    return [_agent_run_row(r) for r in rows]


def get_agent_run(db: Session, run_id: int) -> dict | None:
    row = db.get(AgentRun, run_id)
    return _agent_run_row(row) if row is not None else None
