"""P2-8：历史前缀移出检查点后的模型消息组装与续跑重建。"""

from __future__ import annotations

from app.agent.nodes.agent import _compose_model_messages, _strip_model_prefix
from app.agent.service.resume import ResumeMixin
from app.api import advanced as advanced_module


def test_model_message_prefix_roundtrip():
    state_messages = ["static-core", "question"]
    prefix = ["history-1", "history-2"]

    model_messages = _compose_model_messages(state_messages, prefix, 1)
    assert model_messages == ["static-core", "history-1", "history-2", "question"]

    # 模型调用后追加的工具消息必须保留，历史前缀必须剔除
    model_messages.append("tool-result")
    assert _strip_model_prefix(model_messages, len(prefix), 1) == [
        "static-core",
        "question",
        "tool-result",
    ]


def test_model_message_without_prefix_is_unchanged():
    state_messages = ["static-core", "question"]
    assert _compose_model_messages(state_messages, [], 1) == state_messages
    assert _strip_model_prefix(state_messages, 0, 1) == state_messages


class _Context:
    def compact_conversation(self, _db, _conv_id):
        return None, [{"role": "user", "content": "上一轮问题"}]

    def history_budget_for(self, _db, _conv_id):
        return 10_000


class _Service:
    context = _Context()


def test_rebuild_history_prefix_from_db_rows():
    prefix = ResumeMixin._rebuild_history_prefix(
        _Service(), {"conversation_id": 1}, db=object()
    )

    assert [m.content for m in prefix] == ["上一轮问题"]


def test_rebuild_history_prefix_without_db_returns_empty():
    assert ResumeMixin._rebuild_history_prefix(
        _Service(), {"conversation_id": 1}, db=None
    ) == []


def test_checkpoint_full_messages_restores_history(monkeypatch):
    monkeypatch.setattr(
        advanced_module.repo,
        "get_messages_by_ids",
        lambda db, ids: [
            {"id": 10, "role": "user", "content": "h1", "tool_trace": None},
            {"id": 11, "role": "assistant", "content": "h2", "tool_trace": None},
        ],
    )
    ch = {
        "messages": ["static-core", "question"],
        "history_insert_at": 1,
        "history_row_ids": [10, 11],
    }

    full = advanced_module._checkpoint_full_messages(ch, None, 1)

    assert [getattr(m, "content", m) for m in full] == [
        "static-core",
        "h1",
        "h2",
        "question",
    ]


def test_checkpoint_full_messages_keeps_old_format():
    old = {"messages": ["static-core", "history", "question"]}
    assert advanced_module._checkpoint_full_messages(old, None, 1) == old["messages"]
