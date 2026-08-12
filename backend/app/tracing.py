"""轻量可观测性：线程级 token 用量聚合 + 结构化 trace（JSONL）。

设计说明：
- 不依赖外部服务，trace 写入 meta_dir/traces/YYYY-MM-DD.jsonl（本地、隐私友好）；
- 每个请求线程一个 UsageCollector，构造 ChatOpenAI 时挂为 callback，
  自动聚合该线程内所有 LLM 调用（标题/规划/摘要/记忆/ReAct 各轮）的 token 用量；
- 预留 Langfuse 接入点：后续要上 Langfuse 时，在 write_trace 里加一个分支
  把同一 payload 发给 langfuse，其余代码无需改动。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)

_local = threading.local()


def get_usage_collector() -> "UsageCollector":
    """返回当前线程的用量收集器（惰性创建，线程隔离，并发安全）。"""
    collector = getattr(_local, "collector", None)
    if collector is None:
        collector = UsageCollector()
        _local.collector = collector
    return collector


def reset_usage() -> None:
    """开始一轮 Agent 运行前清零当前线程的用量统计。"""
    get_usage_collector().reset()


def usage_summary() -> dict:
    """返回当前线程累计的 token 用量。"""
    return get_usage_collector().summary()


class UsageCollector(BaseCallbackHandler):
    """LangChain Callback：on_llm_end 时累加 token 用量。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0

    def reset(self) -> None:
        with self._lock:
            self._calls = 0
            self._prompt_tokens = 0
            self._completion_tokens = 0
            self._total_tokens = 0

    def on_llm_end(self, response, **kwargs) -> None:
        """LLM 调用结束时累计 token（OpenAI 兼容接口的 llm_output.token_usage）。"""
        try:
            llm_output = getattr(response, "llm_output", None) or {}
            usage = llm_output.get("token_usage") or {}
            self.add_usage(
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
            )
        except Exception:
            pass

    def add_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """手动累计一次调用（流式调用 usage 走 chunk.usage_metadata，需补记）。"""
        with self._lock:
            self._calls += 1
            self._prompt_tokens += max(0, prompt_tokens)
            self._completion_tokens += max(0, completion_tokens)
            self._total_tokens += max(0, prompt_tokens) + max(0, completion_tokens)

    def summary(self) -> dict:
        with self._lock:
            return {
                "llm_calls": self._calls,
                "prompt_tokens": self._prompt_tokens,
                "completion_tokens": self._completion_tokens,
                "total_tokens": self._total_tokens,
            }


def write_trace(settings, run_id, payload: dict) -> None:
    """把一轮 Agent 运行的结构化 trace 追加到本地 JSONL 文件。"""
    if not getattr(settings, "tracing_enabled", True):
        return
    try:
        trace_dir = settings.meta_dir / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "run_id": run_id,
                **payload,
            },
            ensure_ascii=False,
        )
        with open(
            trace_dir / f"{datetime.now():%Y-%m-%d}.jsonl",
            "a",
            encoding="utf-8",
        ) as f:
            f.write(line + "\n")
    except Exception as exc:
        logger.warning("写 trace 失败：%s", exc)
