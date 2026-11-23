"""上下文统计（context_stats）回归：/context 面板字段与窗口口径。

回归背景：context_stats 内嵌的局部变量曾命名为 `effective`，遮蔽了
runtime_config.effective()；由于 Python 静态作用域，函数里任何
effective(...) 调用都会 UnboundLocalError：
"cannot access local variable 'effective' where it is not associated
with a value" → GET /api/agent/context/{id} 500（上下文面板报
"统计失败"）。本文件锁定该接口的返回值与压缩触发口径。
"""

from __future__ import annotations

import pytest

from app.agent import context as context_mod
from app.agent.context import ContextService


class FakeSettings:
    history_max_messages = 10
    history_max_tokens = 32000
    summary_max_chars = 800
    history_budget_general = 32000
    history_budget_knowledge = 24000
    history_budget_coding = 36000
    history_budget_writing = 36000
    history_budget_translate = 24000


def make_rows(n: int, start_id: int = 1):
    return [
        {"id": start_id + i, "role": "user", "content": f"消息{i}"}
        for i in range(n)
    ]


@pytest.fixture
def svc_state(monkeypatch):
    state = {"rows": [], "summary": None, "up_to": 0}
    monkeypatch.setattr(
        context_mod.repo,
        "list_messages_with_id",
        lambda db, cid, limit=500: list(state["rows"]),
    )
    monkeypatch.setattr(
        context_mod.repo, "get_summary_state", lambda db, cid: (state["summary"], state["up_to"])
    )
    monkeypatch.setattr(context_mod.repo, "get_conversation", lambda db, cid: None)
    monkeypatch.setattr(context_mod.repo, "list_agent_runs", lambda db, **kw: [])
    svc = ContextService(FakeSettings(), chat=None)
    return svc, state


def test_context_stats_below_high_water(svc_state):
    """未触发压缩：发送窗口 = 全部未摘要消息（追溯摘要边界）。"""
    svc, state = svc_state
    state["rows"] = make_rows(12)
    state["summary"] = "旧摘要"
    state["up_to"] = 2

    stats = svc.context_stats(None, 1)

    assert stats["message_count"] == 12
    assert stats["summary_covered_messages"] == 2
    assert stats["effective_history_messages"] == 10  # id>2 共 10 条，未压缩
    assert stats["history_max_messages"] == 10
    assert stats["history_high_water_messages"] == 15
    assert stats["compaction_would_trigger"] is False
    assert stats["estimated_tokens"] > 0
    assert stats["sent_estimate_tokens"] == stats["estimated_tokens"]
    assert stats["trimmed_messages"] == 0
    assert stats["budget_source"] == "设置 history_max_tokens"
    assert stats["recent_runs"] == 0
    assert stats["recent_cache_hit_rate"] is None


def test_context_stats_trigger_reports_post_compaction_count(svc_state):
    """超过 1.5× 高水位：effective = 压缩后保留条数 max(4, 上限)。"""
    svc, state = svc_state
    state["rows"] = make_rows(40)

    stats = svc.context_stats(None, 1)

    assert stats["compaction_would_trigger"] is True
    # 40 条触发压缩 → 保留 max(4, 10) = 10 条
    assert stats["effective_history_messages"] == 10
    assert stats["sent_estimate_tokens"] <= stats["estimated_tokens"]


def test_context_stats_token_trigger_below_message_cap(svc_state):
    """条数未到上限但 token 超水位：统计应反映按 token 压缩后的发送窗口。"""
    svc, state = svc_state
    # 9 条 × 20000 字符 ≈ 45000 token，超过 32000×1.25=40000 水位
    state["rows"] = [
        {"id": i + 1, "role": "user", "content": "x" * 20000} for i in range(9)
    ]

    stats = svc.context_stats(None, 1)

    assert stats["compaction_would_trigger"] is True
    assert stats["estimated_tokens"] == 45000  # 压缩前整窗
    assert stats["sent_estimate_tokens"] == 30000  # 保留最近 6 条
    assert stats["effective_history_messages"] == 6
    assert stats["trimmed_messages"] == 3
    assert stats["trimmed_tokens"] == 15000


def test_context_breakdown_uses_last_run_and_injections(svc_state, monkeypatch):
    """分项估算：系统提示词/工具来自最近 run，注入取最后 user 之后的 system 行。"""
    svc, state = svc_state
    state["rows"] = [
        {"id": 1, "role": "user", "content": "问题一"},
        {"id": 2, "role": "assistant", "content": "回答"},
        {"id": 3, "role": "user", "content": "最新问题"},
        {"id": 4, "role": "system", "content": "【项目记忆】" + "x" * 400},
    ]
    monkeypatch.setattr(
        context_mod.repo,
        "list_agent_runs",
        lambda db, **kw: [
            {
                "token_usage": {
                    "tools_chars": 400,
                    "prompt": {"template_chars": 200, "fragment_chars": 100},
                }
            }
        ],
    )

    stats = svc.context_stats(None, 1)
    b = stats["context_breakdown"]

    assert b["estimated"] is True
    assert b["system_prompt"]["tokens"] == 150  # 300 chars / 2
    assert b["tools"]["tokens"] == 100  # 400 chars / 4
    assert b["question"]["chars"] == len("最新问题")
    assert b["context_injections"]["messages"] == 1
    assert b["context_injections"]["tokens"] > 0
    assert b["history"]["tokens"] >= 0
    assert b["total"] == sum(
        b[key]["tokens"]
        for key in ("system_prompt", "tools", "context_injections", "history", "question")
    )


def test_context_breakdown_degrades_without_runs(svc_state):
    """没有运行记录时仍返回分项结构，提示词/工具为 0，不抛错。"""
    svc, state = svc_state
    state["rows"] = make_rows(3)

    b = svc.context_stats(None, 1)["context_breakdown"]

    assert b["system_prompt"]["tokens"] == 0
    assert b["tools"]["tokens"] == 0
    assert b["history"]["tokens"] >= 0
    assert b["total"] >= b["question"]["tokens"]
