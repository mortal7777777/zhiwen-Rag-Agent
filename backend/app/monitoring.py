"""轻量进程内监控：请求计数、耗时统计、错误率。

生产环境可替换为 Prometheus 客户端 + Grafana，这里先提供 JSON 快照
（/api/metrics），保证零额外依赖。
"""

from __future__ import annotations

import threading
import time


class Metrics:
    """线程安全的指标收集器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start = time.perf_counter()
        self._counts: dict[str, int] = {
            "chat_requests": 0,
            "chat_errors": 0,
            "stream_requests": 0,
            "stream_errors": 0,
        }
        # 耗时统计：{名称: [次数, 累计秒]}
        self._timings: dict[str, list[float]] = {
            "retrieval": [0, 0.0],
            "generation": [0, 0.0],
            "expansion": [0, 0.0],
        }

    def inc(self, name: str, delta: int = 1) -> None:
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + delta

    def record(self, name: str, seconds: float) -> None:
        with self._lock:
            if name not in self._timings:
                self._timings[name] = [0, 0.0]
            self._timings[name][0] += 1
            self._timings[name][1] += seconds

    def snapshot(self) -> dict:
        """返回当前指标快照（含平均值，便于前端展示与排查）。"""
        with self._lock:
            chat_total = self._counts["chat_requests"] + self._counts["stream_requests"]
            chat_errors = self._counts["chat_errors"] + self._counts["stream_errors"]
            return {
                "uptime_seconds": round(time.perf_counter() - self._start, 1),
                "chat_requests": self._counts["chat_requests"],
                "stream_requests": self._counts["stream_requests"],
                "chat_errors": self._counts["chat_errors"],
                "stream_errors": self._counts["stream_errors"],
                "total_requests": chat_total,
                "error_rate": round(chat_errors / chat_total, 4) if chat_total else 0.0,
                "avg_retrieval_ms": self._avg_ms("retrieval"),
                "avg_generation_ms": self._avg_ms("generation"),
                "avg_expansion_ms": self._avg_ms("expansion"),
            }

    def _avg_ms(self, name: str) -> float | None:
        count, seconds = self._timings[name]
        if not count:
            return None
        return round(seconds / count * 1000, 1)


metrics = Metrics()
