"""健康检查接口。"""

from __future__ import annotations

from fastapi import APIRouter

from ..config import get_settings
from ..monitoring import metrics

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.version,
        "uptime_seconds": metrics.snapshot()["uptime_seconds"],
    }


@router.get("/metrics")
def get_metrics() -> dict:
    """进程内监控指标：请求数、错误率、平均耗时。"""
    return metrics.snapshot()
