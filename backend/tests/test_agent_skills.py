"""自创技能（隔离 + 按需共享）：frontmatter 解析、扫描来源、共享/删除安全边界。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import skills as S


def _mk_skill(root: Path, category: str, name: str, extra: str = "") -> Path:
    d = root / category / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: "demo skill"\n{extra}---\n\n# Demo\n\n1. step\n',
        encoding="utf-8",
    )
    return d


def _use_tmp_roots(monkeypatch, tmp_path: Path):
    agent_dir = tmp_path / "agent_skills"
    hermes_dir = tmp_path / "hermes_skills"
    # 包化后路径常量的唯一权威位置在 app.skills.scan（agent_skills 经 scan_mod 引用）
    monkeypatch.setattr(S.scan, "AGENT_SKILLS_DIR", agent_dir)
    monkeypatch.setattr(S.scan, "HERMES_SKILLS_DIR", hermes_dir)
    monkeypatch.setattr(
        S.scan,
        "DEFAULT_ROOTS",
        [
            (S.AGENT_SOURCE, str(agent_dir), "nested"),
            ("hermes", str(hermes_dir), "nested"),
        ],
    )
    return agent_dir, hermes_dir


@pytest.fixture(autouse=True)
def _reset_scan_cache():
    """测试后清空扫描缓存，避免污染同进程内的其他用例。"""
    yield
    S.scan._cache = None
    S.scan._scanned_at = 0.0


def test_parse_frontmatter_reads_origin_and_created_at():
    text = (
        "---\nname: x\ndescription: d\norigin: agent\n"
        "created_at: 2026-09-30\n---\n\n# T\n"
    )
    name, desc, tags, extra = S._parse_frontmatter(text)
    assert name == "x"
    assert desc == "d"
    assert extra["origin"] == "agent"
    assert extra["created_at"] == "2026-09-30"


def test_scan_marks_agent_source_and_shared(tmp_path, monkeypatch):
    agent_dir, hermes_dir = _use_tmp_roots(monkeypatch, tmp_path)
    _mk_skill(agent_dir, "other", "my-flow", "origin: agent\ncreated_at: 2026-09-30\n")

    hits = {s["name"]: s for s in S.scan_skills(force=True)}
    assert hits["my-flow"]["source"] == "agent"
    assert hits["my-flow"]["shared"] is False
    assert hits["my-flow"]["created_at"] == "2026-09-30"

    # 共享副本存在时：合并为一条（自创优先），shared 置位
    _mk_skill(hermes_dir, "other", "my-flow", "origin: agent\ncreated_at: 2026-09-30\n")
    hits = {s["name"]: s for s in S.scan_skills(force=True)}
    assert hits["my-flow"]["source"].startswith("agent")
    assert hits["my-flow"]["shared"] is True
    assert "hermes" in hits["my-flow"]["sources"]


def test_share_unshare_delete_flow(tmp_path, monkeypatch):
    agent_dir, hermes_dir = _use_tmp_roots(monkeypatch, tmp_path)
    _mk_skill(agent_dir, "other", "flow-a", "origin: agent\ncreated_at: 2026-09-30\n")

    out = S.share_agent_skill("flow-a", "other")
    assert out["ok"] and out["updated"] is False
    assert (hermes_dir / "other" / "flow-a" / "SKILL.md").is_file()

    # 再次共享 = 更新副本
    out = S.share_agent_skill("flow-a", "other")
    assert out["updated"] is True

    out = S.unshare_agent_skill("flow-a", "other")
    assert out["updated"] is True
    assert not (hermes_dir / "other" / "flow-a").exists()

    # 非自创副本：拒绝删除
    _mk_skill(hermes_dir, "other", "flow-a")
    with pytest.raises(PermissionError):
        S.unshare_agent_skill("flow-a", "other")

    # 删除自创技能：共享副本一并清理
    _mk_skill(hermes_dir, "other", "flow-a", "origin: agent\n")
    out = S.delete_agent_skill("flow-a", "other")
    assert out["removed_shared"] is True
    assert not (agent_dir / "other" / "flow-a").exists()
    assert not (hermes_dir / "other" / "flow-a").exists()


def test_agent_skill_path_traversal_rejected(tmp_path, monkeypatch):
    agent_dir, hermes_dir = _use_tmp_roots(monkeypatch, tmp_path)
    _mk_skill(agent_dir, "other", "safe", "origin: agent\n")
    with pytest.raises(FileNotFoundError):
        S.delete_agent_skill("../evil", "other")
    # 非法分类被忽略、回退按目录名定位 —— 落点仍受限于技能库内（无穿越）
    out = S.share_agent_skill("safe", "../../etc")
    assert out["ok"]
    assert str(out["target"]).startswith(str(hermes_dir))
    assert (hermes_dir / "other" / "safe" / "SKILL.md").is_file()
    assert (agent_dir / "other" / "safe").exists()


def test_agent_skill_content_preview(tmp_path, monkeypatch):
    agent_dir, _ = _use_tmp_roots(monkeypatch, tmp_path)
    _mk_skill(agent_dir, "other", "flow-b", "origin: agent\n")
    out = S.agent_skill_content("flow-b", "other")
    assert out["ok"]
    assert "# Demo" in out["content"]
    assert out["truncated"] is False
