"""健康检查接口：livez（进程）/ readyz（依赖）/ health（概要 + 可选深探）。"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..monitoring import metrics
from ..single_instance import lock_status

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


def _check_mysql() -> dict:
    """MySQL 探活：SELECT 1；连接失败只返回结果，不抛异常。"""
    from ..db.database import db_ready, engine

    if not db_ready or engine is None:
        return {"ok": False, "error": "数据库未连接"}
    started = time.perf_counter()
    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001 - 探活失败转成状态
        return {"ok": False, "error": str(exc)[:200]}


def _check_opensearch(timeout: float = 2.0) -> dict:
    """OpenSearch 探活：/_cluster/health；短超时，失败不抛异常。"""
    settings = get_settings()
    started = time.perf_counter()
    try:
        from ..network import make_httpx_client

        url = settings.opensearch_url.rstrip("/") + "/_cluster/health"
        with make_httpx_client(timeout=timeout) as client:
            response = client.get(url)
            response.raise_for_status()
            data = response.json()
        return {
            "ok": True,
            "status": data.get("status"),
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001 - 探活失败转成状态
        return {"ok": False, "error": str(exc)[:200]}


def _dependencies() -> dict:
    return {"mysql": _check_mysql(), "opensearch": _check_opensearch()}


@router.get("/livez")
def livez() -> dict:
    """存活探针：只确认进程能响应，不查外部依赖。"""
    return {"status": "ok"}


@router.get("/readyz")
def readyz():
    """就绪探针：MySQL + OpenSearch 都可用才 200；否则 503。"""
    checks = _dependencies()
    ready = all(item.get("ok") for item in checks.values())
    payload = {"status": "ok" if ready else "degraded", "checks": checks}
    if not ready:
        logger.warning("readiness 检查未通过：%s", checks)
        return JSONResponse(status_code=503, content=payload)
    return payload


@router.get("/health")
def health(deep: bool = False) -> dict:
    """概要健康检查；deep=true 时附带依赖探活（与 /readyz 同口径）。"""
    settings = get_settings()
    from ..tools_extra import bash_backend_info

    payload = {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.version,
        "uptime_seconds": metrics.snapshot()["uptime_seconds"],
        # 单实例契约（P0）：本进程是否持有 meta_dir 独占锁。
        # held=false 表示进程没走 lifespan（如测试）或未拿锁（不应出现在线上）。
        "instance_lock": lock_status(),
        # 实际使用的命令 shell 后端（gitbash/wsl/default）与翻译后的 Python：
        # 排查"Windows 路径在 shell 里不存在"这类环境割裂时先看这里。
        "shell": bash_backend_info(settings),
    }
    if deep:
        checks = _dependencies()
        payload["checks"] = checks
        if not all(item.get("ok") for item in checks.values()):
            payload["status"] = "degraded"
    return payload


@router.get("/metrics")
def get_metrics() -> dict:
    """进程内监控指标：请求数、错误率、平均耗时。"""
    return metrics.snapshot()
