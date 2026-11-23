"""派发汇总的纯逻辑：轮数/子任务/模式/错误/降级计数。"""

from __future__ import annotations

from app.agent.nodes.finalize import _dispatch_summary


def test_dispatch_summary_counts_modes_errors_and_downgraded():
    state = {
        "dispatch_rounds": 2,
        "subagent_results": [
            {"name": "a", "mode": "explore", "error": None, "model": "fast", "profile": "scan-fast"},
            {"name": "b", "mode": "explore", "error": "boom", "model": "fast"},
            {"name": "c", "mode": "review", "model": "strong"},
            {"name": "d", "mode": "execute"},
            {"name": "e", "mode": "explore", "cancelled": True},
            {"task": "没有 name 的条目应被忽略"},
        ],
    }
    summary = _dispatch_summary(state, {"dispatch_downgraded": 1})
    assert summary == {
        "rounds": 2,
        "subtasks": 5,
        "modes": {"explore": 3, "review": 1, "execute": 1},
        "models": {"fast": 2, "strong": 1},
        "profiles": {"scan-fast": 1},
        "errors": 1,
        "cancelled": 1,
        "downgraded": 1,
    }


def test_dispatch_summary_defaults_are_zero():
    assert _dispatch_summary({}, None) == {
        "rounds": 0,
        "subtasks": 0,
        "modes": {},
        "models": {},
        "profiles": {},
        "errors": 0,
        "cancelled": 0,
        "downgraded": 0,
    }
