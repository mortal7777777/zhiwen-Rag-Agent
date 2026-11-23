"""按类型模型/预算（Phase A 6.2）：供应商解析、缓存、回退与预算判定。"""

from __future__ import annotations

import pytest

import app.agent.agent as agent_mod
from app.agent.agent import AgentService
from app.agent.nodes.subagent import _type_budget_allows
from app.config import Settings


class _FakeRag:
    def acquire_llm(self) -> None: ...

    def release_llm(self) -> None: ...


class _FakeModel:
    def __init__(self, model_name: str = "fake-model") -> None:
        self.model_name = model_name


@pytest.fixture()
def service():
    return AgentService(Settings(), _FakeRag())


def _provider(**overrides):
    base = {
        "id": "p1",
        "name": "Provider 1",
        "type": "chat",
        "base_url": "https://example.invalid/v1",
        "api_key": "test-key",
        "model": "default-model",
        "models": ["default-model", "fast-model"],
        "enabled": True,
    }
    base.update(overrides)
    return base


def test_provider_model_built_once_and_cached(monkeypatch, service):
    calls = []

    def fake_build(cfg, **kwargs):
        calls.append((cfg, kwargs))
        return _FakeModel(kwargs.get("model") or cfg.get("model"))

    monkeypatch.setattr(
        agent_mod, "get_providers", lambda settings: [_provider()]
    )
    monkeypatch.setattr(agent_mod, "build_chat_model", fake_build)

    first = service.subagent_chat_model("p1", "fast-model", "inherit")
    second = service.subagent_chat_model("p1", "fast-model", "inherit")
    assert first is second
    assert len(calls) == 1
    cfg, kwargs = calls[0]
    assert cfg["id"] == "p1"
    assert kwargs["model"] == "fast-model"
    assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    assert kwargs["extra_body"]["thinking"] == {"type": "enabled"}


def test_unknown_provider_falls_back_to_main_chat(monkeypatch, service):
    monkeypatch.setattr(agent_mod, "get_providers", lambda settings: [])
    main = _FakeModel("main")
    service._chat = main
    assert service.subagent_chat_model("missing", "", "inherit") is main


def test_thinking_off_builds_disabled_model(monkeypatch, service):
    calls = []
    monkeypatch.setattr(
        agent_mod, "get_providers", lambda settings: [_provider()]
    )
    monkeypatch.setattr(
        agent_mod,
        "build_chat_model",
        lambda cfg, **kwargs: calls.append(kwargs) or _FakeModel(),
    )
    service.subagent_chat_model("p1", "", "on", thinking_off=True)
    assert calls[0]["thinking"] == {"type": "disabled"}
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    # 同一供应商/模型但思考模式不同 → 独立缓存
    service.subagent_chat_model("p1", "", "on", thinking_off=False)
    assert len(calls) == 2


def test_empty_provider_uses_active_chat_provider(monkeypatch, service):
    calls = []
    cfg = _provider(id="active", model="active-model")
    monkeypatch.setattr(agent_mod, "chat_provider_config", lambda settings: cfg)
    monkeypatch.setattr(
        agent_mod,
        "build_chat_model",
        lambda cfg, **kwargs: calls.append((cfg, kwargs)) or _FakeModel(),
    )
    service.subagent_chat_model("", "", "inherit")
    assert calls[0][0]["id"] == "active"
    assert calls[0][1]["model"] == "active-model"


def test_type_budget_allows():
    assert _type_budget_allows(None, 99) is True
    assert _type_budget_allows(0, 99) is True
    assert _type_budget_allows(3, 0) is True
    assert _type_budget_allows(3, 2) is True
    assert _type_budget_allows(3, 3) is False
