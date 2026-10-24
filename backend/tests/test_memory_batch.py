"""记忆整合分批（2026-10-23）。

回归：旧实现用 lines[:12000] 截断，而列表是"旧→新"，被丢掉的是最新记忆，
与提示词"以录入日期较新为准"直接矛盾。现在按行切块分批，全部成功才落库。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.agent import context as context_mod
from app.agent.context import ConsolidateOutput, ContextService


class FakeSettings:
    memory_enabled = True
    memory_consolidate_threshold = 10

    def __init__(self, meta_dir):
        self.meta_dir = meta_dir


def _mem(mid: int, size: int = 600) -> dict:
    created = datetime(2026, 1, 1) + timedelta(days=mid)
    return {
        "id": mid,
        "content": f"记忆{mid}-" + "x" * size,
        "category": "project",
        "status": "active",
        "hit_count": 0,
        "created_at": created,
        "updated_at": created,
    }


@pytest.fixture()
def env(monkeypatch, tmp_path):
    state = {"memories": [], "updates": [], "meta": {}, "prompts": []}
    monkeypatch.setattr(
        context_mod.repo, "list_all_memories",
        lambda db, limit=2000: state["memories"],
    )
    monkeypatch.setattr(
        context_mod.repo, "list_memories",
        lambda db, limit=500, **kw: [m for m in state["memories"] if m["status"] == "active"],
    )

    def _update(db, mid, content=None, category=None, status=None):
        state["updates"].append({"id": mid, "content": content, "status": status})
        for m in state["memories"]:
            if m["id"] == mid:
                if content is not None:
                    m["content"] = content
                if status is not None:
                    m["status"] = status

    monkeypatch.setattr(context_mod.repo, "update_memory", _update)
    monkeypatch.setattr(
        context_mod.repo, "set_meta",
        lambda db, key, value: state["meta"].__setitem__(key, value),
    )
    state["service"] = ContextService(FakeSettings(tmp_path), chat=None)
    return state


def _chunks(prompts: list[str]) -> list[str]:
    return [p.split("记忆列表：\n", 1)[1] for p in prompts]


def test_large_memory_list_is_chunked_and_keeps_newest(env):
    env["memories"] = [_mem(i) for i in range(1, 31)]
    calls = {"n": 0}

    def fake(schema, prompt):
        calls["n"] += 1
        env["prompts"].append(prompt)
        return ConsolidateOutput(facts=[], archived_ids=[], summary=f"批{calls['n']}")

    env["service"]._structured = fake
    env["service"].consolidate_memories(object())

    assert len(env["prompts"]) >= 2
    for chunk in _chunks(env["prompts"]):
        assert len(chunk) <= 12000
    # 最新记忆只能出现在靠后的批次（旧实现会把它截掉）
    assert "记忆30-" in env["prompts"][-1]
    assert all("记忆30-" not in p for p in env["prompts"][:-1])
    assert env["meta"]["memory_summary"] == f"批{len(env['prompts'])}"


def test_failure_in_later_batch_applies_nothing(env):
    env["memories"] = [_mem(i) for i in range(1, 31)]
    calls = {"n": 0}

    def fake(schema, prompt):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("model down")
        return ConsolidateOutput(
            facts=[{"source_ids": [1, 2], "category": "project", "content": "合并结果"}],
            archived_ids=[],
            summary="批1",
        )

    env["service"]._structured = fake
    result = env["service"].consolidate_memories(object())

    assert result == {"merged": 0, "archived": 0, "summary": None}
    assert env["updates"] == []
    assert "memory_last_consolidated_at" not in env["meta"]


def test_duplicate_fact_across_batches_is_applied_once(env):
    env["memories"] = [_mem(i) for i in range(1, 31)]

    def fake(schema, prompt):
        return ConsolidateOutput(
            facts=[{"source_ids": [1, 2], "category": "project", "content": "同一结论"}],
            archived_ids=[],
            summary="画像",
        )

    env["service"]._structured = fake
    result = env["service"].consolidate_memories(object())

    assert result["merged"] == 1
    assert result["archived"] == 1
    writes = [u for u in env["updates"] if u["content"] == "同一结论"]
    assert len(writes) == 1
