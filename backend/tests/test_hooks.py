"""Hooks 与原生 checkpointer 序列化单测。"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.db import repository as repo
from app.hooks import run_hooks
from app.native_checkpoint import _sanitize, messages_to_history_rows


def test_pre_tool_use_hook_deny(tmp_path, monkeypatch):
    script = tmp_path / "hook.py"
    script.write_text(
        "import sys, json\n"
        "d = json.load(sys.stdin)\n"
        "print(json.dumps({'decision': 'deny', 'reason': 'test-block'}))\n",
        encoding="utf-8",
    )
    hooks = json.dumps(
        [
            {
                "name": "block",
                "command": f"python {script}",
                "events": ["pre_tool_use"],
                "timeout": 15,
            }
        ]
    )
    monkeypatch.setattr(repo, "get_meta", lambda db, key: hooks)
    results = run_hooks(object(), "pre_tool_use", "write_file", {"path": "x.txt"})
    assert len(results) == 1
    assert results[0]["decision"] == "deny"
    assert results[0]["reason"] == "test-block"


def test_hook_skips_unrelated_event(monkeypatch):
    hooks = json.dumps(
        [
            {
                "name": "post",
                "command": "python -c \"print('{}')\"",
                "events": ["post_tool_use"],
            }
        ]
    )
    monkeypatch.setattr(repo, "get_meta", lambda db, key: hooks)
    assert run_hooks(object(), "pre_tool_use", "bash", {}) == []


def test_sanitize_replaces_unserializable():
    class Obj:
        pass

    out = _sanitize({"a": [1, "x"], "b": Obj(), "c": {"d": 2}})
    assert out["a"] == [1, "x"]
    assert out["b"] == {"__unserializable__": "Obj"}
    assert out["c"] == {"d": 2}


def test_messages_to_history_rows():
    rows = messages_to_history_rows(
        [
            SystemMessage(content="system"),
            HumanMessage(content="问题"),
            AIMessage(content="回答"),
        ]
    )
    assert rows == [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "回答"},
    ]
