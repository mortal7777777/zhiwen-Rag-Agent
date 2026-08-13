"""项目记忆按 CLAUDE.md 语义分层加载。"""

from __future__ import annotations

from pathlib import Path

from app.project_memory import load_project_memory


def test_load_memory_walks_parents_and_user_global(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".myragagent").mkdir(parents=True)
    (home / ".myragagent" / "AGENTS.md").write_text(
        "USER-GLOBAL", encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    root = tmp_path / "proj"
    sub = root / "a" / "b"
    sub.mkdir(parents=True)
    (root / "AGENTS.md").write_text("ROOT-MEMORY", encoding="utf-8")
    (sub / "AGENTS.md").write_text("SUB-MEMORY", encoding="utf-8")

    text = load_project_memory(object(), override_path=str(sub))
    assert text is not None
    assert text.index("SUB-MEMORY") < text.index("ROOT-MEMORY")
    assert text.endswith("USER-GLOBAL") or "USER-GLOBAL" in text


def test_load_memory_missing_user_global(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    empty = tmp_path / "empty"
    empty.mkdir()
    # 临时目录在仓库内：向上回溯会命中仓库根 AGENTS.md（符合 CLAUDE.md 语义），
    # 但用户级全局文件已被隔离，不应出现 USER-GLOBAL
    text = load_project_memory(object(), override_path=str(empty))
    assert "USER-GLOBAL" not in (text or "")
