"""UsageCollector：token 与缓存命中/未命中指标。"""

from __future__ import annotations

from app.tracing import get_usage_collector


def test_cache_usage_aliases():
    collector = get_usage_collector()
    collector.reset()
    collector.add_cache_usage(
        {"prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 20}
    )
    s = collector.summary()
    assert s["cache_hit_tokens"] == 100
    assert s["cache_miss_tokens"] == 20


def test_cache_usage_cached_tokens_fallback():
    collector = get_usage_collector()
    collector.reset()
    collector.add_cache_usage({"prompt_tokens_details": {"cached_tokens": 42}})
    assert collector.summary()["cache_hit_tokens"] == 42


def test_cached_tokens_only_derives_miss_from_prompt_total():
    """OpenAI 风格只给 cached_tokens：未命中要按输入总量反推，
    否则成本漏掉全部未缓存输入、命中率虚高到 100%。"""
    collector = get_usage_collector()
    collector.reset()
    collector.add_cache_usage(
        {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 900}}
    )
    s = collector.summary()
    assert s["cache_hit_tokens"] == 900
    assert s["cache_miss_tokens"] == 100


def test_langchain_usage_metadata_cache_read():
    collector = get_usage_collector()
    collector.reset()
    collector.add_cache_usage(
        {"input_tokens": 1000, "input_token_details": {"cache_read": 750}}
    )
    s = collector.summary()
    assert s["cache_hit_tokens"] == 750
    assert s["cache_miss_tokens"] == 250
