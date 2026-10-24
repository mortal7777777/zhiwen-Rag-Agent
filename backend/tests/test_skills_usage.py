"""技能用量账本：计数、原子写、pin。"""

from __future__ import annotations

import json

from app.skills import usage


def _use_tmp(tmp_path, monkeypatch):
    root = tmp_path / "agent_skills"
    root.mkdir()
    monkeypatch.setattr("app.skills.scan.AGENT_SKILLS_DIR", root)
    return root


def test_record_view_and_patch(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    usage.record_view("demo-skill")
    usage.record_view("demo-skill")
    usage.record_patch("demo-skill")

    data = json.loads(usage.ledger_path().read_text(encoding="utf-8"))
    row = data["demo-skill"]
    assert row["view_count"] == 2
    assert row["use_count"] == 2
    assert row["patch_count"] == 1
    assert row["state"] == "active"
    assert row["pinned"] is False
    assert row["last_viewed_at"].endswith("+00:00")
    assert row["created_by"] == "agent"


def test_pin_and_snapshot(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    usage.record_view("demo-skill")
    snapshot = usage.set_pinned("demo-skill", True)
    assert snapshot["demo-skill"]["pinned"] is True
    assert usage.snapshot()["demo-skill"]["pinned"] is True


def test_ledger_is_valid_json_after_updates(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    for name in ("a", "b", "a"):
        usage.record_view(name)
    text = usage.ledger_path().read_text(encoding="utf-8")
    parsed = json.loads(text)
    assert set(parsed) == {"a", "b"}
    assert parsed["a"]["view_count"] == 2
