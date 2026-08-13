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
