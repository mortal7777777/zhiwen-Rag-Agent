"""记忆整合整理的行为单测。

锁三件此前出错的事：
1. 交给模型的列表必须是"按录入时间从旧到新"（提示词的前提），此前用
   updated_at DESC（最新在前），与提示词的自述相反；
2. 合并主记录取"录入时间最新"的那条，而不是 max(id)；
3. 每次整合写审计快照（此前只有一个时间戳，出问题无法回溯）；
   且"归档非合并项"要给出可观测告警（历史事故：46 条压成 2 条）。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.agent import context as context_mod
from app.agent.context import ConsolidateOutput, ContextService


def _mem(mid: int, content: str, category: str, created: datetime, hits: int = 0) -> dict:
    return {
        "id": mid,
        "content": content,
        "category": category,
        "status": "active",
        "hit_count": hits,
        "created_at": created,
        "updated_at": created,
    }


class FakeSettings:
    memory_enabled = True
    memory_top_k = 3
    memory_min_score = 0.35
    memory_max_tokens = 600
    memory_recall_scan_limit = 500
    memory_empty_fallback = 2
    memory_consolidate_interval_hours = 24
    memory_consolidate_threshold = 10

    def __init__(self, meta_dir):
        self.meta_dir = meta_dir


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """三件套：可控的记忆列表 + 捕获写操作 + 捕获提示词。"""
    state: dict = {"memories": [], "updates": [], "meta": {}, "prompt": ""}
    monkeypatch.setattr(
        context_mod.repo, "list_all_memories",
        lambda db, limit=2000: state["memories"],
    )
    monkeypatch.setattr(
        context_mod.repo, "list_memories",
        lambda db, limit=500, **kw: [m for m in state["memories"] if m["status"] == "active"],
    )

    def _update_memory(db, mid, content=None, category=None, status=None):
        state["updates"].append({"id": mid, "content": content, "category": category, "status": status})
        # 与真实 repository 一致：真的改到列表上（否则 active_after 等派生值算不准）
        for m in state["memories"]:
            if m["id"] == mid:
                if content is not None:
                    m["content"] = content
                if category is not None:
                    m["category"] = category
                if status is not None:
                    m["status"] = status
        return None

    monkeypatch.setattr(context_mod.repo, "update_memory", _update_memory)
    monkeypatch.setattr(
        context_mod.repo, "set_meta",
        lambda db, key, value: state["meta"].__setitem__(key, value),
    )
    service = ContextService(FakeSettings(tmp_path), chat=None)
    state["service"] = service
    state["tmp_path"] = tmp_path
    return state


def _stub_structured(state, out: ConsolidateOutput):
    def _fake(schema, prompt):
        state["prompt"] = prompt
        return out

    state["service"]._structured = _fake


def test_prompt_lists_memories_oldest_first_with_input_dates(env):
    env["memories"] = [
        _mem(9, "较新的事实", "project", datetime(2026, 9, 20)),
        _mem(3, "较旧的事实", "preference", datetime(2026, 8, 1), hits=5),
    ]
    _stub_structured(env, ConsolidateOutput(facts=[], archived_ids=[], summary="画像"))

    env["service"].consolidate_memories(object())
    prompt = env["prompt"]

    # 旧在前、新在后（与提示词自述的前提一致，此前是反的）
    assert prompt.index("较旧的事实") < prompt.index("较新的事实")
    assert "录入 2026-08-01" in prompt and "录入 2026-09-20" in prompt
    assert "从旧到新" in prompt
    # 归档语义已收紧：不许因为"琐碎/命中少"归档
    assert "不要" in prompt and "不作为归档依据" in prompt


def test_merge_keeps_the_newest_source_as_primary(env):
    """主记录按 created_at 取最新，而不是 max(id)。"""
    env["memories"] = [
        _mem(50, "旧说法：用户在做对比整理", "project", datetime(2026, 9, 1)),
        _mem(7, "新说法：主线是「同一个前提，两条不同的路径」", "project", datetime(2026, 9, 25)),
    ]
    _stub_structured(
        env,
        ConsolidateOutput(
            facts=[
                {
                    "source_ids": [50, 7],
                    "category": "project",
                    "content": "用户在做对比整理，主线是「同一个前提，两条不同的路径」",
                }
            ],
            archived_ids=[],
            summary="",
        ),
    )

    result = env["service"].consolidate_memories(object())
    by_id = {u["id"]: u for u in env["updates"]}
    assert by_id[7]["content"]  # id 小但 created_at 新 → 它是主记录（被写入新内容）
    assert by_id[50]["status"] == "archived"
    assert result["merged"] == 1
    assert result["archived"] == 1


def test_explicit_archive_ids_are_applied(env):
    env["memories"] = [
        _mem(1, "用户住在成都", "profile", datetime(2026, 8, 1)),
        _mem(2, "用户目前定居北京", "profile", datetime(2026, 9, 1)),
    ]
    _stub_structured(
        env,
        ConsolidateOutput(facts=[], archived_ids=[1], summary="用户现居北京"),
    )

    result = env["service"].consolidate_memories(object())
    assert {u["id"] for u in env["updates"] if u["status"] == "archived"} == {1}
    assert result["archived"] == 1
    assert env["meta"]["memory_summary"] == "用户现居北京"


def test_audit_snapshot_is_written(env):
    env["memories"] = [
        _mem(3, "较旧的事实", "preference", datetime(2026, 8, 1), hits=5),
        _mem(9, "较新的事实", "project", datetime(2026, 9, 20)),
    ]
    _stub_structured(env, ConsolidateOutput(facts=[], archived_ids=[9], summary="画像"))

    env["service"].consolidate_memories(object())

    files = list((env["tmp_path"] / "memory_audit").glob("*.jsonl"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert record["active_before"] == 2
    assert record["active_after"] == 1
    assert record["archived_ids"] == [9]
    assert record["merged_source_ids"] == []
    # 变更前快照可用于回滚（内容 + 命中次数 + 录入时间）
    assert {r["id"] for r in record["before"]} == {3, 9}
    assert record["before"][0]["content"] == "较旧的事实"
    assert record["before"][0]["hit_count"] == 5


def test_archiving_non_merged_items_logs_warning(env, caplog):
    """归档量远超合并量 = 删除而非去重，必须留下可观测告警。"""
    env["memories"] = [
        _mem(i, f"事实 {i}", "project", datetime(2026, 9, i)) for i in range(1, 7)
    ]
    _stub_structured(
        env,
        ConsolidateOutput(facts=[], archived_ids=[1, 2, 3, 4, 5], summary=""),
    )

    with caplog.at_level("WARNING", logger="app.agent.context"):
        env["service"].consolidate_memories(object())

    assert any("疑似过度删除" in r.message for r in caplog.records)


def test_normal_dedup_does_not_warn(env, caplog):
    """正常去重（归档项都来自合并）不告警。"""
    env["memories"] = [
        _mem(1, "用户在做对比整理（说法一）", "project", datetime(2026, 9, 1)),
        _mem(2, "用户在做对比整理（说法二）", "project", datetime(2026, 9, 2)),
        _mem(3, "用户住在成都", "profile", datetime(2026, 9, 3)),
    ]
    _stub_structured(
        env,
        ConsolidateOutput(
            facts=[{"source_ids": [1, 2], "category": "project", "content": "用户在做对比整理"}],
            archived_ids=[],
            summary="",
        ),
    )

    with caplog.at_level("WARNING", logger="app.agent.context"):
        env["service"].consolidate_memories(object())

    assert not any("疑似过度删除" in r.message for r in caplog.records)


def test_archived_count_ignores_ids_that_were_not_active(env):
    """模型给出不存在/已归档的 id 时不虚报。

    审计记录与"过度删除"告警都依赖这个数字；把请求集合当成结果会让
    这两件"让问题可见"的东西失真。
    """
    env["memories"] = [
        _mem(1, "事实一", "project", datetime(2026, 9, 1)),
        _mem(2, "事实二", "project", datetime(2026, 9, 2)),
    ]
    _stub_structured(
        env,
        ConsolidateOutput(facts=[], archived_ids=[2, 999], summary=""),
    )

    result = env["service"].consolidate_memories(object())
    assert result["archived"] == 1                      # 999 从未生效，不计入
    assert [u["id"] for u in env["updates"] if u["status"] == "archived"] == [2]

    record = json.loads(
        next((env["tmp_path"] / "memory_audit").glob("*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert record["archived_ids"] == [2]
    assert record["active_after"] == 1


def test_no_active_memories_is_a_noop(env, monkeypatch):
    env["memories"] = []
    result = env["service"].consolidate_memories(object())
    assert result == {"merged": 0, "archived": 0, "summary": None}
    assert env["updates"] == []
