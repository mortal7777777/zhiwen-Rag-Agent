"""AGENTS.md 导出：分节结构 + 人工维护区保留（对齐 CLAUDE.md 语义）。"""

from __future__ import annotations

import app.project_memory as pm


class FakeSettings:
    project_memory_file = ""


def _mem(i: int, category: str, content: str, updated: str = "2026-09-21T10:00:00"):
    return {
        "id": i,
        "content": content,
        "category": category,
        "status": "active",
        "source_conversation_id": None,
        "created_at": updated,
        "updated_at": updated,
    }


def _prepare(tmp_path, monkeypatch, memories, summary="用户画像摘要"):
    path = tmp_path / "AGENTS.md"
    monkeypatch.setattr(pm.repo, "get_meta", lambda db, key: summary)
    monkeypatch.setattr(pm.repo, "list_memories", lambda db, limit=300: memories)
    monkeypatch.setattr(pm, "project_memory_path", lambda settings: path)
    return path


def test_export_groups_memories_into_sections(tmp_path, monkeypatch):
    path = _prepare(
        tmp_path,
        monkeypatch,
        [
            _mem(1, "project", "项目事实 A"),
            _mem(2, "decision", "决定 B"),
            _mem(3, "preference", "偏好 C"),
            _mem(4, "lesson", "教训 D"),
        ],
    )
    pm.export_project_memory(FakeSettings(), None)
    text = path.read_text(encoding="utf-8")

    assert "## 用户画像" in text and "用户画像摘要" in text
    assert "## 项目与工作" in text
    # project 与 decision 归入同一节
    head = text.index("## 项目与工作")
    section = text[head : text.index("## 偏好与习惯")]
    assert "项目事实 A" in section and "决定 B" in section
    assert "偏好 C" in text[text.index("## 偏好与习惯") : text.index("## 经验与教训")]
    assert "教训 D" in text[text.index("## 经验与教训") : text.index("## 其他")]
    # 不再有旧的"长期事实记忆"流水账小节
    assert "## 长期事实记忆" not in text


def test_export_preserves_manual_section(tmp_path, monkeypatch):
    path = _prepare(tmp_path, monkeypatch, [_mem(1, "project", "项目事实 A")])
    path.write_text(
        "# AGENTS.md\n\n## 用户画像\n旧的画像\n\n"
        "## 人工维护\n- 项目约定：用 conda pytorch_env\n- 不要提交 data/\n",
        encoding="utf-8",
    )
    pm.export_project_memory(FakeSettings(), None)
    text = path.read_text(encoding="utf-8")

    assert "用 conda pytorch_env" in text
    assert "不要提交 data/" in text
    # 人工维护区排在自动内容之前：加载端有字符截断，手写规则最不该被切掉
    assert text.index("## 人工维护") < text.index("## 用户画像")


def test_manual_section_ignores_inline_mention(tmp_path, monkeypatch):
    """正文里顺带提到标记文字，不应被当作区块起点。

    曾用 `text.find(marker)` 匹配 → 文件头的说明行命中，第二次导出把
    说明行之后的全部内容当成"人工维护区"，导出结果自我吞噬。
    """
    path = _prepare(tmp_path, monkeypatch, [_mem(1, "project", "事实 A")])
    path.write_text(
        "# AGENTS.md\n\n> 说明：下面的 ## 人工维护 一节由人维护。\n\n"
        "## 人工维护\n- 规则 X\n\n"
        f"{pm._MANUAL_END}\n\n## 用户画像\n旧画像\n",
        encoding="utf-8",
    )
    pm.export_project_memory(FakeSettings(), None)
    text = path.read_text(encoding="utf-8")

    assert "- 规则 X" in text          # 人工区被保留
    assert "事实 A" in text            # 自动区生成正常
    assert text.count("## 用户画像") == 1  # 未被重复吞并


def test_export_creates_manual_placeholder_when_missing(tmp_path, monkeypatch):
    path = _prepare(tmp_path, monkeypatch, [])
    pm.export_project_memory(FakeSettings(), None)
    text = path.read_text(encoding="utf-8")

    assert pm._MANUAL_MARKER in text
    assert "手工维护" in text


def test_export_applies_section_quota(tmp_path, monkeypatch):
    memories = [_mem(i, "project", f"项目事实 {i}") for i in range(20)]
    path = _prepare(tmp_path, monkeypatch, memories)
    pm.export_project_memory(FakeSettings(), None)
    text = path.read_text(encoding="utf-8")

    section = text[text.index("## 项目与工作") : text.index("## 偏好与习惯")]
    lines = [x for x in section.splitlines() if x.startswith("- ")]
    assert len(lines) == 16  # _EXPORT_SECTIONS 里 project+decision 的配额


def test_export_truncates_overlong_line(tmp_path, monkeypatch):
    path = _prepare(tmp_path, monkeypatch, [_mem(1, "project", "长" * 500)])
    pm.export_project_memory(FakeSettings(), None)
    text = path.read_text(encoding="utf-8")

    line = next(x for x in text.splitlines() if x.startswith("- 长"))
    assert line.endswith("…")
    assert len(line) <= pm._LINE_MAX + 3


def test_export_unchanged_content_not_rewritten(tmp_path, monkeypatch):
    path = _prepare(tmp_path, monkeypatch, [_mem(1, "project", "项目事实 A")])
    pm.export_project_memory(FakeSettings(), None)
    first = path.read_text(encoding="utf-8")
    res = pm.export_project_memory(FakeSettings(), None)
    assert res.get("unchanged") is True
    assert path.read_text(encoding="utf-8") == first
