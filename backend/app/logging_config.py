"""日志与请求上下文：stdout + 轮转文件，每条日志带 request_id。

本机直跑（非容器）时日志落 `backend/logs/app.log`（10MB × 5 轮转）；
Docker 下同时保留 stdout，由容器日志收集。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path

from starlette.datastructures import MutableHeaders

REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")
BACKEND_ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = BACKEND_ROOT / "logs" / "app.log"
_LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(request_id)s] %(name)s: %(message)s"


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = REQUEST_ID.get()
        return True


class JsonFormatter(logging.Formatter):
    """可选结构化日志（LOG_JSON=1）：每行一个 JSON，便于采集/检索。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """重置根日志处理器，避免 uvicorn --reload 重复挂载。"""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    formatter = (
        JsonFormatter()
        if os.environ.get("LOG_JSON", "0") == "1"
        else logging.Formatter(_LOG_FORMAT)
    )
    stream = logging.StreamHandler(sys.stdout)
    rotating = RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    for handler in (stream, rotating):
        handler.setFormatter(formatter)
        handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
        try:
            existing.close()
        except Exception:  # noqa: BLE001 - 关闭失败不应阻断启动
            pass
    root.addHandler(stream)
    root.addHandler(rotating)


class RequestContextMiddleware:
    """纯 ASGI 中间件：注入 X-Request-ID 与上下文，不缓冲 SSE。"""

    def __init__(self, app) -> None:
        self.app = app
        self.logger = logging.getLogger("app.request")

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"")
        request_id = incoming.decode("latin-1").strip()[:64] or uuid.uuid4().hex[:16]
        token = REQUEST_ID.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()

        async def send_with_id(message):
            if message.get("type") == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            self.logger.exception(
                "%s %s 未处理异常", scope.get("method", "?"), scope.get("path", "?")
            )
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.logger.info(
                "%s %s %.0fms",
                scope.get("method", "?"),
                scope.get("path", "?"),
                elapsed_ms,
            )
            REQUEST_ID.reset(token)
