"""Agent 决策运行记录接口：问题、计划、工具轨迹、耗时、状态。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..db import repository as repo

router = APIRouter(tags=["runs"])


@router.get("/runs")
def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    conversation_id: int | None = None,
    db: Session = Depends(get_db),
) -> list[dict]:
    """查看最近的 Agent 运行记录（最近的在最前）。"""
    return repo.list_agent_runs(
        db,
        limit=limit,
        conversation_id=conversation_id,
    )


@router.get("/runs/{run_id}/trace")
def run_trace(run_id: int) -> dict:
    """读取某次运行的完整结构化 trace（问题/计划/工具轨迹/用量）。"""
    settings = get_settings()
    trace_dir = settings.meta_dir / "traces"
    if not trace_dir.exists():
        return {}
    for f in sorted(trace_dir.glob("*.jsonl"), reverse=True):
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                if str(data.get("run_id")) == str(run_id):
                    return data
        except Exception:
            continue
    return {}
