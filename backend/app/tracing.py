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
_aux_local = threading.local()


def get_usage_collector() -> "UsageCollector":
    """返回当前线程的用量收集器（惰性创建，线程隔离，并发安全）。"""
    collector = getattr(_local, "collector", None)
    if collector is None:
        collector = UsageCollector()
        _local.collector = collector
    return collector


def get_aux_usage_collector() -> "UsageCollector":
    """辅助调用的独立用量收集器（线程隔离）。

    标题生成、计划生成、会话摘要、记忆召回、思考摘要等辅助 LLM 调用
    每次使用独立消息、不共享主循环的前缀缓存，若计入同一收集器会
    把整体缓存命中率稀释到无意义。单独统计，便于准确评估主循环
    的缓存命中率与总成本。
    """
    collector = getattr(_aux_local, "collector", None)
    if collector is None:
        collector = UsageCollector()
        _aux_local.collector = collector
    return collector


def reset_usage() -> None:
    """开始一轮 Agent 运行前清零当前线程的用量统计（含辅助调用收集器）。"""
    get_usage_collector().reset()
    get_aux_usage_collector().reset()


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
        self._cache_hit_tokens = 0
        self._cache_miss_tokens = 0

    def reset(self) -> None:
        with self._lock:
            self._calls = 0
            self._prompt_tokens = 0
            self._completion_tokens = 0
            self._total_tokens = 0
            self._cache_hit_tokens = 0
            self._cache_miss_tokens = 0

    def on_llm_end(self, response, **kwargs) -> None:
        """LLM 调用结束时累计 token（OpenAI 兼容接口的 llm_output.token_usage）。"""
        try:
            llm_output = getattr(response, "llm_output", None) or {}
            usage = llm_output.get("token_usage") or {}
            self.add_usage(
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
            )
            self.add_cache_usage(usage)
        except Exception:
            pass

    def add_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """手动累计一次调用（流式调用 usage 走 chunk.usage_metadata，需补记）。"""
        with self._lock:
            self._calls += 1
            self._prompt_tokens += max(0, prompt_tokens)
            self._completion_tokens += max(0, completion_tokens)
            self._total_tokens += max(0, prompt_tokens) + max(0, completion_tokens)

    def add_cache(self, hit_tokens: int, miss_tokens: int) -> None:
        """手动补记前缀缓存命中/未命中 token（流式调用场景）。"""
        with self._lock:
            self._cache_hit_tokens += max(0, hit_tokens)
            self._cache_miss_tokens += max(0, miss_tokens)

    def add_cache_usage(self, usage: dict) -> None:
        """从 usage_metadata 提取缓存命中/未命中 token（兼容多家字段名）。

        各 provider 的字段名差异：
        - DeepSeek 官方: prompt_tokens_details.cached_tokens
        - LangChain usage_metadata: input_token_details.cache_read
        - OpenAI 官方: prompt_tokens_details.cached_tokens
        - Anthropic: cache_creation_input_tokens / cache_read_input_tokens
        """
        if not usage:
            return
        hit = usage.get("prompt_cache_hit_tokens")
        miss = usage.get("prompt_cache_miss_tokens")
        if hit is None and isinstance(usage.get("prompt_tokens_details"), dict):
            details = usage["prompt_tokens_details"]
            hit = details.get("cached_tokens")
        # LangChain usage_metadata 结构：input_token_details.cache_read
        if hit is None and isinstance(usage.get("input_token_details"), dict):
            details = usage["input_token_details"]
            hit = details.get("cache_read")
            if hit is not None:
                total_in = int(usage.get("input_tokens") or 0)
                miss = max(0, total_in - int(hit))
        # Anthropic 风格：cache_read_input_tokens / cache_creation_input_tokens
        if hit is None:
            read_tok = usage.get("cache_read_input_tokens")
            create_tok = usage.get("cache_creation_input_tokens")
            if read_tok is not None or create_tok is not None:
                hit = int(read_tok or 0) + int(create_tok or 0)
                total_in = int(usage.get("input_tokens") or 0)
                miss = max(0, total_in - int(hit))
        if miss is None and hit is None:
            return
        self.add_cache(int(hit or 0), int(miss or 0))

    def summary(self) -> dict:
        with self._lock:
            return {
                "llm_calls": self._calls,
                "prompt_tokens": self._prompt_tokens,
                "completion_tokens": self._completion_tokens,
                "total_tokens": self._total_tokens,
                "cache_hit_tokens": self._cache_hit_tokens,
                "cache_miss_tokens": self._cache_miss_tokens,
            }


def usage_summary_with_aux() -> dict:
    """主循环用量 + 辅助调用用量（合并输出，aux 字段单独标注）。

    主循环（agent 决策/工具循环）共享前缀缓存，命中率才有意义；
    辅助调用（标题/计划/摘要/记忆/子代理）每次独立请求，命中率为 0。
    aux 字段单独列出，避免稀释主循环命中率，同时保留真实总成本。
    """
    main = get_usage_collector().summary()
    aux = get_aux_usage_collector().summary()
    combined = dict(main)
    combined["aux_llm_calls"] = aux["llm_calls"]
    combined["aux_prompt_tokens"] = aux["prompt_tokens"]
    combined["aux_completion_tokens"] = aux["completion_tokens"]
    combined["aux_total_tokens"] = aux["total_tokens"]
    combined["total_with_aux"] = (
        main["total_tokens"] + aux["total_tokens"]
    )
    return combined


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
