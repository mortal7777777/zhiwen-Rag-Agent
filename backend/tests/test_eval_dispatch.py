"""派发 ROI 评测脚本的纯函数测试（不连后端）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "eval" / "eval_dispatch.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("eval_dispatch", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_questions_file_has_unique_ids_and_roi():
    mod = _load_module()
    questions = mod.load_questions()
    assert len(questions) >= 10
    ids = [q["id"] for q in questions]
    assert len(ids) == len(set(ids))
    assert {q.get("roi") for q in questions} <= {"inline", "dispatch"}
    assert all(q.get("expect") for q in questions)


def test_extract_run_metrics_reads_dispatch_and_usage():
    mod = _load_module()
    run = {
        "status": "ok",
        "latency_ms": 1234,
        "answer_len": 500,
        "token_usage": {
            "total_tokens": 1000,
            "aux_total_tokens": 200,
            "subagent_llm_calls": 4,
            "timings": {"subagent_ms": 800},
            "dispatch": {"rounds": 2, "subtasks": 3, "errors": 1},
        },
        "tool_trace": [
            {"name": "read_file"},
            {"name": "subagent"},
            {"name": "subagent"},
        ],
    }
    metrics = mod.extract_run_metrics(run)
    assert metrics == {
        "status": "ok",
        "latency_ms": 1234,
        "answer_len": 500,
        "total_tokens": 1000,
        "aux_total_tokens": 200,
        "subagent_llm_calls": 4,
        "dispatch_rounds": 2,
        "dispatch_subtasks": 3,
        "dispatch_errors": 1,
        "subagent_ms": 800,
        "tool_calls": 1,
        "subagent_runs": 2,
    }


def test_summarize_and_markdown_compare_configs():
    mod = _load_module()
    rows = [
        {
            "config": "subagents-on", "id": "a", "category": "x", "roi": "dispatch",
            "status": "ok", "latency_ms": 100, "total_tokens": 500,
            "aux_total_tokens": 100, "subagent_llm_calls": 3, "dispatch_rounds": 1,
            "dispatch_subtasks": 2, "score": 2,
        },
        {
            "config": "subagents-on", "id": "b", "category": "x", "roi": "inline",
            "status": "ok", "latency_ms": 200, "total_tokens": 700,
            "aux_total_tokens": 0, "subagent_llm_calls": 0, "dispatch_rounds": 0,
            "dispatch_subtasks": 0, "score": 1,
        },
        {
            "config": "subagents-off", "id": "a", "category": "x", "roi": "dispatch",
            "status": "ok", "latency_ms": 300, "total_tokens": 400,
            "aux_total_tokens": 0, "subagent_llm_calls": 0, "dispatch_rounds": 0,
            "dispatch_subtasks": 0, "score": 2,
        },
    ]
    summary = mod.summarize(rows)
    assert summary["subagents-on"]["count"] == 2
    assert summary["subagents-on"]["avg_latency_ms"] == 150.0
    assert summary["subagents-on"]["dispatched_questions"] == 1
    assert summary["subagents-on"]["avg_score"] == 1.5
    assert summary["subagents-off"]["avg_subagent_calls"] == 0.0

    markdown = mod.to_markdown(rows, {"generated_at": "t", "base_url": "u", "limit": 2, "judge": True})
    assert "派发 ROI 评测报告" in markdown
    assert "| subagents-on | 2 | 150.0 |" in markdown
    assert "对比结论" in markdown
    assert "满分 2" in markdown
