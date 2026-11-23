"""子代理上下文包增强（Phase B 6.4）：git/清单/关键词/摘要与总量封顶。"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from app.agent.context_pack import MAX_CONTEXT_PACK_CHARS, build_context_pack


def test_pack_detects_manifest_keyword_files_and_summary(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "zhiwen-web",
                "scripts": {"dev": "vite", "build": "vite build"},
                "dependencies": {"vue": "^3.5.0", "axios": "^1.7.0"},
            }
        ),
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("def pelican_bicycle():\n    return 'ok'\n", encoding="utf-8")
    (src / "other.py").write_text("print('unrelated')\n", encoding="utf-8")

    pack = build_context_pack(
        tmp_path,
        task="请定位 pelican bicycle 的实现并说明调用方",
        mode="explore",
        tool_names=["list_dir", "read_file"],
        summary_text="此前讨论过 pelican 的原型实现。",
    )
    assert len(pack) <= MAX_CONTEXT_PACK_CHARS
    assert "package.json" in pack and "zhiwen-web" in pack
    assert "pelican" in pack and "src/app.py" in pack
    assert "近期对话摘要" in pack
    assert "Git 状态" not in pack
    assert "write_file" not in pack
    assert "本模式目标" in pack


@pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")
def test_pack_includes_git_status_and_diff(tmp_path):
    def git(*args):
        subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "init")
    target.write_text("value = 2\n", encoding="utf-8")

    pack = build_context_pack(
        tmp_path, task="review app.py", mode="review", tool_names=["read_file"]
    )
    assert "Git 状态" in pack
    assert "app.py" in pack
    assert "git diff --stat" in pack


def test_pack_sections_are_trimmed(tmp_path):
    for i in range(80):
        (tmp_path / f"file_{i:03d}_with_a_long_name.py").write_text(
            "x = 1\n", encoding="utf-8"
        )
    pack = build_context_pack(
        tmp_path,
        task="check all files",
        mode="explore",
        tool_names=["read_file"],
        summary_text="很久以前的摘要。" * 800,
    )
    assert len(pack) <= MAX_CONTEXT_PACK_CHARS + 20
    assert "已截断" in pack


def test_pack_total_cap_applies(monkeypatch, tmp_path):
    from app.agent import context_pack as cp

    monkeypatch.setattr(cp, "_listing", lambda workspace: "目录" * 1000)
    monkeypatch.setattr(cp, "_git_section", lambda workspace: "改动" * 1000)
    monkeypatch.setattr(cp, "_manifest_section", lambda workspace: "清单" * 1000)
    pack = cp.build_context_pack(tmp_path, task="x", mode="explore", tool_names=["read_file"])
    assert len(pack) <= MAX_CONTEXT_PACK_CHARS + 20
    assert "上下文包已截断" in pack


def test_pack_without_task_still_has_tools_and_listing(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    pack = build_context_pack(
        tmp_path, task="", mode="research", tool_names=None
    )
    assert "README.md" in pack
    assert "knowledge_base_search" in pack
