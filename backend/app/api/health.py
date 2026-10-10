"""健康检查接口。"""

from __future__ import annotations

from fastapi import APIRouter

from ..config import get_settings
from ..monitoring import metrics
from ..single_instance import lock_status

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.version,
        "uptime_seconds": metrics.snapshot()["uptime_seconds"],
        # 单实例契约（P0）：本进程是否持有 meta_dir 独占锁。
        # held=false 表示进程没走 lifespan（如测试）或未拿锁（不应出现在线上）。
        "instance_lock": lock_status(),
    }


@router.get("/metrics")
def get_metrics() -> dict:
    """进程内监控指标：请求数、错误率、平均耗时。"""
    return metrics.snapshot()
