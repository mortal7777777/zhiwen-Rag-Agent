"""聊天页快捷切换：权限模式 / 思考强度端点。

直接调用处理函数（FastAPI 依赖就是普通默认参数），隔离 runtime_config 的
进程内覆盖缓存与 repo.set_meta 落库。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import runtime_config as rc
from app.api import settings as settings_api
from app.db import repository as repo


@pytest.fixture()
def isolated(monkeypatch):
    monkeypatch.setattr(rc, "_overrides", {})
    monkeypatch.setattr(repo, "set_meta", lambda *args, **kwargs: None)
    return rc


def _providers() -> list[dict]:
    base = {
        "base_url": "https://example.com",
        "api_key": "sk-test",
        "model": "demo",
        "models": ["demo"],
        "enabled": True,
        "thinking_enabled": True,
        "thinking_effort": "high",
    }
    return [
        {**base, "id": "chat-a", "name": "A", "type": "chat"},
        {**base, "id": "chat-b", "name": "B", "type": "chat"},
        {**base, "id": "vision-a", "name": "V", "type": "vision"},
    ]


def _fake_refresh():
    calls: list[int] = []
    return SimpleNamespace(refresh_chat_only=lambda: calls.append(1)), calls


# ---------------- 权限模式 ----------------


def test_switch_permission_mode_roundtrip(isolated):
    res = settings_api.switch_tool_permission_mode(
        {"mode": "acceptedits"}, db=object()
    )

    assert res["tool_permission_mode"] == "acceptedits"
    assert rc._overrides["tool_permission_mode"] == "acceptedits"


def test_switch_permission_mode_rejects_unknown(isolated):
    with pytest.raises(HTTPException) as exc:
        settings_api.switch_tool_permission_mode({"mode": "yolo"}, db=object())

    assert exc.value.status_code == 422


# ---------------- 思考强度 ----------------


def test_switch_thinking_updates_only_active_chat_provider(isolated):
    rc._overrides["providers"] = _providers()
    rc._overrides["active_chat_provider"] = "chat-b"
    agent, agent_calls = _fake_refresh()
    service, service_calls = _fake_refresh()

    res = settings_api.switch_chat_provider_thinking(
        {"thinking_effort": "max"}, db=object(), agent=agent, service=service
    )

    assert res["provider_id"] == "chat-b"
    assert res["thinking_enabled"] is True
    assert res["thinking_effort"] == "max"
    saved = {p["id"]: p for p in rc._overrides["providers"]}
    assert saved["chat-b"]["thinking_effort"] == "max"
    assert saved["chat-a"]["thinking_effort"] == "high"
    assert saved["vision-a"]["thinking_effort"] == "high"
    assert agent_calls and service_calls, "切换后要刷新对话模型缓存"


def test_switch_thinking_can_disable_without_losing_effort(isolated):
    rc._overrides["providers"] = _providers()
    rc._overrides["active_chat_provider"] = "chat-a"
    agent, _ = _fake_refresh()
    service, _ = _fake_refresh()

    res = settings_api.switch_chat_provider_thinking(
        {"thinking_enabled": False}, db=object(), agent=agent, service=service
    )

    assert res["thinking_enabled"] is False
    assert res["thinking_effort"] == "high"
    saved = {p["id"]: p for p in rc._overrides["providers"]}
    assert saved["chat-a"]["thinking_enabled"] is False
    assert saved["chat-a"]["thinking_effort"] == "high"


def test_switch_thinking_rejects_unknown_effort(isolated):
    rc._overrides["providers"] = _providers()
    rc._overrides["active_chat_provider"] = "chat-a"
    agent, _ = _fake_refresh()
    service, _ = _fake_refresh()

    with pytest.raises(HTTPException) as exc:
        settings_api.switch_chat_provider_thinking(
            {"thinking_effort": "ultra"}, db=object(), agent=agent, service=service
        )

    assert exc.value.status_code == 422


def test_switch_thinking_404_when_active_missing(isolated):
    rc._overrides["providers"] = _providers()
    rc._overrides["active_chat_provider"] = "missing"
    agent, _ = _fake_refresh()
    service, _ = _fake_refresh()

    with pytest.raises(HTTPException) as exc:
        settings_api.switch_chat_provider_thinking(
            {"thinking_effort": "low"}, db=object(), agent=agent, service=service
        )

    assert exc.value.status_code == 404
