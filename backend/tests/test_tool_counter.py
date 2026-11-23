"""引用编号器必须由 AgentState 注入并被主工具共享（避免与 merge 重号）。"""

from __future__ import annotations

from types import SimpleNamespace

import app.agent.tools as tools_pkg
from app.agent.tools import build_tools


class _FakeRag:
    settings = SimpleNamespace()


def _patch_factories(monkeypatch, captured: dict) -> None:
    def fake_kb(*args, **kwargs):
        captured["kb_counter"] = args[1]
        return object()

    def fake_web(*args, **kwargs):
        captured["web_counter"] = args[3]
        return object()

    monkeypatch.setattr(tools_pkg, "make_knowledge_base_tool", fake_kb)
    monkeypatch.setattr(tools_pkg, "make_web_search_tool", fake_web)
    monkeypatch.setattr(tools_pkg, "make_add_document_tool", lambda *a, **k: object())


def test_build_tools_shares_injected_counter(monkeypatch):
    captured: dict = {}
    _patch_factories(monkeypatch, captured)
    counter = [7]
    build_tools(
        _FakeRag(),
        True,
        True,
        "ddg",
        "",
        3,
        vision=None,
        skills_enabled=False,
        counter=counter,
    )
    assert captured["kb_counter"] is counter
    assert captured["web_counter"] is counter


def test_build_tools_creates_one_counter_when_omitted(monkeypatch):
    captured: dict = {}
    _patch_factories(monkeypatch, captured)
    build_tools(
        _FakeRag(),
        True,
        True,
        "ddg",
        "",
        3,
        vision=None,
        skills_enabled=False,
    )
    assert captured["kb_counter"] is captured["web_counter"]
    assert captured["kb_counter"] == [0]
