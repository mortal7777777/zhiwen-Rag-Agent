"""tool_trace 落库截断：TEXT 列溢出会让整轮消息与运行记录一起丢（2026-09-24 实测）。"""

import json

from app.db.sanitize import (
    sanitize_sources,
    sanitize_token_usage,
    truncate_tool_trace,
)


def _entry(name: str, size: int = 100) -> dict:
    return {
        "name": name,
        "arguments": {"path": "x.html"},
        "summary": "s" * size,
        "step": 1,
        "duration_ms": 12,
    }


def test_under_limit_unchanged():
    trace = [_entry("read_file"), _entry("list_dir")]
    assert truncate_tool_trace(trace) == trace


def test_empty_and_none_passthrough():
    assert truncate_tool_trace(None) is None
    assert truncate_tool_trace([]) == []


def test_over_limit_keeps_head_and_marks_omitted():
    trace = [_entry(f"tool_{i}", size=5000) for i in range(40)]
    out = truncate_tool_trace(trace, max_bytes=20000)
    assert out[0] is trace[0]  # 首元素原样保留
    assert out[-1]["_truncated"] is True
    assert out[-1]["omitted"] == len(trace) - len(out) + 1
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= 20000


def test_oversized_first_item_keeps_pairing_fields():
    """role='tool' 行靠 tt[0] 的 tool_call_id/name 重建配对，绝不能丢。"""
    first = {"tool_call_id": "call_abc", "name": "read_file", "summary": "z" * 90000}
    out = truncate_tool_trace([first, _entry("bash")], max_bytes=20000)
    assert isinstance(out[0], dict)
    assert out[0]["tool_call_id"] == "call_abc"
    assert out[0]["name"] == "read_file"
    assert len(out[0]["summary"]) < 2000


def test_max_items_cap():
    trace = [_entry(f"t{i}") for i in range(500)]
    out = truncate_tool_trace(trace, max_items=10)
    assert len(out) <= 11
    assert out[-1]["_truncated"] is True


def test_token_usage_truncates_long_lists():
    usage = {"chain_hashes": ["h" * 64 for _ in range(3000)], "llm_calls": 3}
    out = sanitize_token_usage(usage, max_bytes=20000)
    assert len(out["chain_hashes"]) < 3000
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= 20000


def test_sanitize_sources_trims_content_field():
    sources = [{"index": 1, "content": "c" * 5000, "title": "t"}]
    out = sanitize_sources(sources)
    assert len(out[0]["content"]) < 1000
    assert out[0]["title"] == "t"
