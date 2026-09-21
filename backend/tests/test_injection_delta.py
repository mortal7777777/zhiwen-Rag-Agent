"""D 块增量注入与模型窗口建模单测。

- `_window_has_same`：同内容副本判定（D 块"变化才追加"的判据）；
- `context_window_for` / `_clamp_to_context_window`：历史预算不超模型窗口。
"""

from __future__ import annotations

from app.agent.context import ContextService, context_window_for
from app.agent.utils import _window_has_same


class FakeSettings:
    model_context_window = 0
    context_window_reserve = 32000
    providers: list = []
    active_chat_provider = ""


def _row(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def test_window_has_same_exact_match():
    rows = [_row("system", "项目记忆（AGENTS.md）：\n内容 A"), _row("user", "问题")]
    assert _window_has_same(rows, "项目记忆（AGENTS.md）：\n内容 A") is True


def test_window_has_same_changed_content_not_found():
    rows = [_row("system", "项目记忆（AGENTS.md）：\n内容 A")]
    assert _window_has_same(rows, "项目记忆（AGENTS.md）：\n内容 B") is False


def test_window_has_same_role_filtered():
    """非 system 行（如用户恰好复述了同样文本）不参与判据。"""
    rows = [_row("user", "同样的文本")]
    assert _window_has_same(rows, "同样的文本") is False
    assert _window_has_same(rows, "同样的文本", role="user") is True


def test_window_has_same_empty_is_true():
    """空内容视为"无需注入"，避免把空块追加进链。"""
    assert _window_has_same([], "") is True


def test_context_window_defaults_and_variants():
    s = FakeSettings()
    # 默认模型名 → 保守 128K
    assert context_window_for(s) == 128_000

    s.providers = [{"id": "p1", "model": "deepseek-v4-flash[1m]"}]
    s.active_chat_provider = "p1"
    assert context_window_for(s) == 1_000_000

    s.model_context_window = 32_000
    assert context_window_for(s) == 32_000  # 显式配置优先


def test_budget_clamped_to_window():
    s = FakeSettings()
    s.model_context_window = 128_000
    s.context_window_reserve = 32_000
    service = ContextService(s, chat=None)

    assert service._clamp_to_context_window(96_000) == 96_000      # 未超窗
    assert service._clamp_to_context_window(200_000) == 96_000     # 夹到 128K−32K
    # 极小窗口也不至于把预算夹到不可用
    s.model_context_window = 20_000
    assert service._clamp_to_context_window(96_000) == 8_000
