"""会话滚动摘要的窗口行为：粘滞起点 + 高水位触发（缓存前缀稳定性回归）。

背景：压缩会把窗口起点前移、改写历史开头，provider 前缀缓存从第一行起
失效。修复前每轮都丢几条最旧消息，导致每轮缓存断链；本文件锁定
"未触发时窗口逐字节不变、触发时一次回收足够多"的行为。
"""

from __future__ import annotations

import pytest

from app.agent import context as context_mod
from app.agent.context import ContextService


class FakeSettings:
    history_max_messages = 60
    history_max_tokens = 32000
    summary_max_chars = 800
    history_budget_general = 32000
    history_budget_knowledge = 24000
    history_budget_coding = 36000
    history_budget_writing = 36000
    history_budget_translate = 24000


def make_rows(n: int, start_id: int = 1, content: str = "消息"):
    return [
        {
            "id": start_id + i,
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"{content}{i}",
        }
        for i in range(n)
    ]


@pytest.fixture
def svc_state(monkeypatch):
    state = {"rows": [], "summary": None, "up_to": 0, "saved": []}
    monkeypatch.setattr(
        context_mod.repo,
        "list_messages_with_id",
        lambda db, cid, limit=500: list(state["rows"]),
    )
    monkeypatch.setattr(
        context_mod.repo, "get_summary_state", lambda db, cid: (state["summary"], state["up_to"])
    )
    monkeypatch.setattr(context_mod.repo, "get_conversation", lambda db, cid: None)

    def save_summary(db, cid, summary, up_to_id):
        state["summary"] = summary
        state["up_to"] = up_to_id
        state["saved"].append((summary, up_to_id))

    monkeypatch.setattr(context_mod.repo, "save_summary", save_summary)
    svc = ContextService(FakeSettings(), chat=None)
    monkeypatch.setattr(svc, "_summarize", lambda batch, existing: "摘要")
    return svc, state


def test_no_compaction_below_high_water(svc_state):
    """窗口 80 条 < 1.5×上限(90)：不压缩、不写摘要。"""
    svc, state = svc_state
    state["rows"] = make_rows(80)
    summary, window = svc.compact_conversation(None, 1)
    assert summary is None
    assert len(window) == 80
    assert state["saved"] == []


def test_compaction_at_high_water_keeps_max(svc_state):
    """窗口 91 条 > 90：压到上限 60 条，摘要边界推进到被丢弃的最后一条。"""
    svc, state = svc_state
    state["rows"] = make_rows(91)
    summary, window = svc.compact_conversation(None, 1)
    assert summary == "摘要"
    assert len(window) == 60
    assert state["up_to"] == 31  # rows[30] 的 id，即被并入摘要的最后一条
    assert state["saved"] == [("摘要", 31)]
    assert window[0]["id"] == 32


def test_window_sticky_across_turns(svc_state):
    """压缩后多轮内窗口起点不变（回归：修复前每轮前移导致缓存每轮失效）。"""
    svc, state = svc_state
    state["rows"] = make_rows(91)
    _, first = svc.compact_conversation(None, 1)
    front = first[0]["id"]

    # 之后两轮各新增 20/30 条，窗口仍不超过 1.5 倍上限 -> 起点必须不变
    state["rows"] = make_rows(111)
    _, second = svc.compact_conversation(None, 1)
    assert second[0]["id"] == front
    state["rows"] = make_rows(121)  # 窗口恰好 90 条，等于高水位不触发
    _, third = svc.compact_conversation(None, 1)
    assert third[0]["id"] == front
    assert len(state["saved"]) == 1  # 只有第一次压缩写了摘要


def test_compaction_resumes_past_high_water(svc_state):
    """再超水位时压缩继续推进，且只总结新增区间、不重复总结。"""
    svc, state = svc_state
    state["rows"] = make_rows(91)
    svc.compact_conversation(None, 1)
    state["rows"] = make_rows(152)  # 窗口 121 条 > 90
    summary, window = svc.compact_conversation(None, 1)
    assert len(window) == 60
    assert window[0]["id"] == 93  # 152-60+1
    assert state["up_to"] == 92
    assert len(state["saved"]) == 2


def test_token_high_water_triggers_compaction(svc_state):
    """条数未超但 token 超 1.25×预算：同样压缩到上限条数。"""
    svc, state = svc_state
    state["rows"] = make_rows(70, content="字" * 1200)  # 每条约 800 token
    summary, window = svc.compact_conversation(None, 1)
    assert summary == "摘要"
    assert len(window) == 60
    assert len(state["saved"]) == 1


def test_few_giant_rows_skip_compaction(svc_state):
    """条数很少但单条超长（cutoff=0）：不压缩，交给 trim 裁剪。"""
    svc, state = svc_state
    state["rows"] = make_rows(20, content="字" * 12000)
    summary, window = svc.compact_conversation(None, 1)
    assert summary is None
    assert len(window) == 20
    assert state["saved"] == []


def test_summarize_failure_does_not_advance(svc_state, monkeypatch):
    """摘要生成失败：边界不推进（窗口不截断、不写库），避免静默丢历史。"""
    svc, state = svc_state
    monkeypatch.setattr(svc, "_summarize", lambda batch, existing: None)
    state["rows"] = make_rows(91)
    summary, window = svc.compact_conversation(None, 1)
    assert summary is None
    assert len(window) == 91
    assert state["saved"] == []


class BudgetSettings:
    history_max_messages = 400
    history_max_tokens = 64000
    history_budget_general = 64000
    history_budget_knowledge = 64000
    history_budget_coding = 96000
    history_budget_writing = 96000
    history_budget_translate = 64000
    summary_max_chars = 800


def test_history_budget_follows_template_category(monkeypatch):
    """模板类别 -> 预算：coding 用更大预算（压缩与裁剪同一口径）。"""
    svc = ContextService(BudgetSettings(), chat=None)

    class _Conv:
        template_id = 3

    class _Tpl:
        category = "coding"

    monkeypatch.setattr(context_mod.repo, "get_conversation", lambda db, cid: _Conv())
    monkeypatch.setattr(context_mod.repo, "get_template", lambda db, tid: _Tpl())
    assert svc.history_budget_for(None, 1) == 96000


def test_history_budget_falls_back_without_template(monkeypatch):
    """无模板 / 未知类别 -> 兜底 history_max_tokens。"""
    svc = ContextService(BudgetSettings(), chat=None)
    monkeypatch.setattr(context_mod.repo, "get_conversation", lambda db, cid: None)
    assert svc.history_budget_for(None, 1) == 64000

    class _Conv:
        template_id = 9

    class _Tpl:
        category = "learning"

    monkeypatch.setattr(context_mod.repo, "get_conversation", lambda db, cid: _Conv())
    monkeypatch.setattr(context_mod.repo, "get_template", lambda db, tid: _Tpl())
    assert svc.history_budget_for(None, 1) == 64000
