"""Agent 决策运行记录接口：问题、计划、工具轨迹、耗时、状态、用量统计。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..db import repository as repo
from ..db.models import AgentRun

router = APIRouter(tags=["runs"])

# 估算成本（元/百万 token，deepseek-chat 量级；仅用于仪表盘参考，非账单）
COST_PER_MTOKENS = {"input_miss": 2.0, "input_hit": 0.5, "output": 8.0}


@router.get("/runs/stats")
def runs_stats(
    days: int = Query(default=7, ge=1, le=90),
    period: str = Query(default="", pattern="^(today|3d|7d|all)?$"),
    db: Session = Depends(get_db),
) -> dict:
    """用量统计仪表盘：tokens / 缓存命中率 / 估算成本 / 延迟 + 每日趋势。

    时间范围：period=today|3d|7d|all（自然日语义：today=今日 00:00 起，
    3d/7d=含今天往前 N 天，all=全部）；period 为空时兼容旧 days 参数。
    成本按 deepseek-chat 量级单价估算（命中/未命中/输出分开计价），
    供观察趋势用，不是账单。
    """
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        since = today_start
    elif period == "3d":
        since = today_start - timedelta(days=2)
    elif period == "7d":
        since = today_start - timedelta(days=6)
    elif period == "all":
        since = None
    else:
        since = now - timedelta(days=days)
    query = select(AgentRun).order_by(AgentRun.created_at.desc()).limit(2000)
    if since is not None:
        query = query.where(AgentRun.created_at >= since)
    rows = db.execute(query).scalars().all()
    total = {
        "runs": len(rows),
        "ok_runs": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_hit": 0,
        "cache_miss": 0,
        "latency_ms": 0,
    }
    daily: dict[str, dict] = {}
    for row in rows:
        if row.status == "ok":
            total["ok_runs"] += 1
        usage = row.token_usage if isinstance(row.token_usage, dict) else _safe_json(row.token_usage)
        hit = _to_int(usage, ("cache_hit_tokens", "prompt_cache_hit_tokens"))
        miss = _to_int(usage, ("cache_miss_tokens", "prompt_cache_miss_tokens"))
        inp = _to_int(usage, ("input_tokens", "prompt_tokens"))
        out = _to_int(usage, ("output_tokens", "completion_tokens"))
        # usage 里 input 通常是 hit+miss 的总和；缺 hit/miss 时按 miss=input 计
        if hit == 0 and miss == 0 and inp:
            miss = inp
        total["input_tokens"] += inp
        total["output_tokens"] += out
        total["cache_hit"] += hit
        total["cache_miss"] += miss
        total["latency_ms"] += row.latency_ms or 0
        day = (row.created_at or datetime.now()).strftime("%m-%d")
        slot = daily.setdefault(
            day,
            {
                "runs": 0,
                "ok_runs": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_hit": 0,
                "cache_miss": 0,
                "cost": 0.0,
            },
        )
        slot["runs"] += 1
        if row.status == "ok":
            slot["ok_runs"] += 1
        slot["input_tokens"] += inp
        slot["output_tokens"] += out
        slot["cache_hit"] += hit
        slot["cache_miss"] += miss
        slot["cost"] += _estimate_cost(miss, hit, out)

    hit_all = total["cache_hit"] + total["cache_miss"]
    cost = _estimate_cost(
        total["cache_miss"], total["cache_hit"], total["output_tokens"]
    )
    return {
        "days": days,
        "period": period or ("all" if since is None else f"{days}d"),
        **total,
        "avg_latency_ms": round(total["latency_ms"] / total["runs"]) if total["runs"] else 0,
        "ok_rate": round(total["ok_runs"] / total["runs"], 3) if total["runs"] else None,
        "cache_hit_rate": round(total["cache_hit"] / hit_all, 3) if hit_all else None,
        "estimated_cost_yuan": round(cost, 3),
        "cost_unit_note": "估算（deepseek-chat 量级单价，非账单）",
        "daily": [{"date": day, **stats} for day, stats in sorted(daily.items())],
    }


def _estimate_cost(miss: float, hit: float, out: float) -> float:
    """按 deepseek-chat 量级单价估算成本（元）。"""
    return (
        miss / 1_000_000 * COST_PER_MTOKENS["input_miss"]
        + hit / 1_000_000 * COST_PER_MTOKENS["input_hit"]
        + out / 1_000_000 * COST_PER_MTOKENS["output"]
    )


def _safe_json(raw) -> dict:
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _to_int(usage: dict, keys: tuple[str, ...]) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return 0


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
