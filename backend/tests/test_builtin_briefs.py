"""内置 Brief 模板技能（Phase B 6.8）：扫描、始终启用与内容结构。"""

from __future__ import annotations

import pytest

import app.skills.prefs as prefs_mod
import app.skills.scan as scan_mod
from app.skills.scan import BUILTIN_SKILLS_DIR, _iter_skill_dirs, _parse_frontmatter

EXPECTED = {
    "brief/multi-book-research",
    "brief/code-review",
    "brief/execute-file-change",
}


def _builtin_skills() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for skill_dir, category in _iter_skill_dirs(BUILTIN_SKILLS_DIR, "nested"):
        path = skill_dir / "SKILL.md"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        name, description, tags, extra = _parse_frontmatter(text)
        found[name] = {
            "category": category,
            "description": description,
            "tags": tags,
            "extra": extra,
            "text": text,
        }
    return found


def test_builtin_brief_files_exist_with_frontmatter():
    skills = _builtin_skills()
    assert set(skills) == EXPECTED
    for name, item in skills.items():
        assert item["category"] == "brief"
        assert item["description"]
        assert "brief" in item["tags"]
        assert item["extra"].get("when_to_use")
        assert "## 模板" in item["text"]
        assert "验收" in item["text"]
        assert "禁止" in item["text"]


def test_scan_exposes_builtin_briefs_and_marks_enabled(monkeypatch):
    monkeypatch.setattr(
        scan_mod, "DEFAULT_ROOTS", [("zhiwen", str(BUILTIN_SKILLS_DIR), "nested")]
    )
    monkeypatch.setattr(scan_mod, "_cache", None)
    monkeypatch.setattr(scan_mod, "_scanned_at", 0.0)
    skills = scan_mod.scan_skills(force=True)
    names = {s["name"] for s in skills}
    assert EXPECTED <= names
    for skill in skills:
        if skill["name"] in EXPECTED:
            assert skill["source"] == "zhiwen"
            assert skill["enabled"] is True
            assert skill["path"].endswith("SKILL.md")


def test_briefs_stay_enabled_under_custom_prefs(monkeypatch):
    monkeypatch.setattr(
        prefs_mod,
        "_prefs",
        {"enabled": {"pdf"}, "hidden": set(), "custom": True},
    )
    enabled = prefs_mod._get_cached_prefs()["enabled"]
    assert {"pdf"} | EXPECTED <= enabled


def test_hidden_brief_is_not_force_enabled(monkeypatch):
    monkeypatch.setattr(
        prefs_mod,
        "_prefs",
        {
            "enabled": set(),
            "hidden": {"brief/code-review"},
            "custom": True,
        },
    )
    cached = prefs_mod._get_cached_prefs()
    assert "brief/code-review" not in cached["enabled"]
    assert "brief/multi-book-research" in cached["enabled"]


def test_skill_view_loads_builtin_brief():
    from app.agent.tools.skill import make_skill_view_tool

    enabled = prefs_mod._get_cached_prefs()["enabled"]
    tool = make_skill_view_tool(None, enabled_ids=enabled)
    result = tool.invoke({"name": "brief/code-review"})
    assert result["ok"] is True
    assert "Brief 模板：只读代码审查" in result["content"]
    assert "path:line" in result["content"]
