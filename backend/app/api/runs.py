"""Agent 决策运行记录接口：问题、计划、工具轨迹、耗时、状态、用量统计。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..db import repository as repo
from ..db.models import AgentRun
from ..runtime_config import chat_provider_config

router = APIRouter(tags=["runs"])

# 供应商单价：USD / 百万 token，元组为 (input_miss, input_hit, output)。
# 来源：OpenCode Go 官方价目（DeepSeek V4.1 Flash / V4 Flash / V4 Pro 峰谷价）。
# 仅用于仪表盘估算，不是账单；未知模型走保守兜底。
PRICING_USD_PER_MTOKENS: dict[str, dict[str, tuple[float, float, float]]] = {
    "deepseek-v4.1-flash": {
        "off_peak": (0.15, 0.003, 0.60),
        "peak": (0.30, 0.006, 1.20),
    },
    "deepseek-v4-flash": {
        "off_peak": (0.15, 0.003, 0.60),
        "peak": (0.30, 0.006, 1.20),
    },
    "deepseek-v4-pro": {
        "off_peak": (0.66, 0.022, 1.98),
        "peak": (1.32, 0.044, 3.96),
    },
}
# 未知型号兜底（DeepSeek V3 量级，USD）：宁可略高估，不低估
DEFAULT_PRICING = {"off_peak": (0.27, 0.07, 1.10), "peak": (0.27, 0.07, 1.10)}

_CN_TZ = ZoneInfo("Asia/Shanghai")


def _normalize_model(model) -> str:
    """模型名归一化：小写、去掉 [1m] 等窗口后缀。"""
    return str(model or "").strip().lower().split("[", 1)[0]


def _active_model() -> str:
    """当前激活供应商的模型名（历史运行未记录 model 时的近似）。"""
    try:
        return _normalize_model(
            (chat_provider_config(get_settings()) or {}).get("model")
        )
    except Exception:
        return ""


def _is_peak(dt: datetime | None) -> bool:
    """OpenCode Go 峰时：UTC 周一至周五 01:00-04:00、06:00-10:00。

    run.created_at 是本地时间（默认按 Asia/Shanghai 解释）。
    """
    if dt is None:
        return False
    try:
        utc = dt.replace(tzinfo=_CN_TZ).astimezone(timezone.utc)
    except Exception:
        return False
    if utc.weekday() >= 5:
        return False
    return 1 <= utc.hour < 4 or 6 <= utc.hour < 10


def _pricing_for(model, dt: datetime | None) -> tuple[float, float, float]:
    key = _normalize_model(model)
    table = PRICING_USD_PER_MTOKENS.get(key)
    if table is None:
        table = PRICING_USD_PER_MTOKENS.get(_active_model())
    if table is None:
        table = DEFAULT_PRICING
    return table["peak" if _is_peak(dt) else "off_peak"]



@router.get("/runs/stats")
def runs_stats(
    days: int = Query(default=7, ge=1, le=90),
    period: str = Query(default="", pattern="^(today|3d|7d|all)?$"),
    db: Session = Depends(get_db),
) -> dict:
    """用量统计仪表盘：tokens / 缓存命中率 / 估算成本 / 延迟 + 每日趋势。

    时间范围：period=today|3d|7d|all（自然日语义：today=今日 00:00 起，
    3d/7d=含今天往前 N 天，all=全部）；period 为空时兼容旧 days 参数。
    成本按每次运行记录的模型单价与峰谷时段估算，再按 USD→CNY 汇率换算，
    供观察趋势用，不是账单。
    """
    now = datetime.now()
    rate = float(getattr(get_settings(), "usd_cny_rate", 7.2) or 7.2)
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
    total_cost = 0.0
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
        run_cost = _estimate_cost(
            miss,
            hit,
            out,
            usage.get("model"),
            row.created_at,
            rate,
        )
        total_cost += run_cost
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
        slot["cost"] += run_cost

    hit_all = total["cache_hit"] + total["cache_miss"]
    return {
        "days": days,
        "period": period or ("all" if since is None else f"{days}d"),
        **total,
        "avg_latency_ms": round(total["latency_ms"] / total["runs"]) if total["runs"] else 0,
        "ok_rate": round(total["ok_runs"] / total["runs"], 3) if total["runs"] else None,
        "cache_hit_rate": round(total["cache_hit"] / hit_all, 3) if hit_all else None,
        "estimated_cost_yuan": round(total_cost, 3),
        "cost_unit_note": (
            f"估算（按模型单价与峰谷时段，USD→CNY {rate:g}，非账单）"
        ),
        "daily": [{"date": day, **stats} for day, stats in sorted(daily.items())],
    }


def _estimate_cost(
    miss: float,
    hit: float,
    out: float,
    model: str | None = None,
    created_at: datetime | None = None,
    usd_cny: float = 7.2,
) -> float:
    """按模型单价与峰谷时段估算成本（人民币元）。"""
    input_miss, input_hit, output = _pricing_for(model, created_at)
    usd = (
        miss / 1_000_000 * input_miss
        + hit / 1_000_000 * input_hit
        + out / 1_000_000 * output
    )
    return usd * usd_cny


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
