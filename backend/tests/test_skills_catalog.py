"""技能 L0 全量索引：名称不隐藏、预算降级、稳定排序与签名。"""

from __future__ import annotations

from app.skills.catalog import build_skill_index


def _entry(name: str, group: str = "documents", desc: str = "处理文档", **extra) -> dict:
    item = {
        "name": name,
        "description": desc,
        "when_to_use": "",
        "source": "claude",
        "group": group,
        "platform_ok": True,
    }
    item.update(extra)
    return item


def test_all_names_visible_even_when_budget_is_tiny():
    entries = [
        _entry(f"doc-{i}", "documents", "很长的描述 " * 20)
        for i in range(8)
    ] + [_entry("ascii-art", "media", "生成 ASCII 艺术")]
    index = build_skill_index(entries, budget=120)
    assert index.visible == 9
    assert index.names_only > 0
    # 名称永不隐藏：超预算时只降 names-only
    for entry in entries:
        assert entry["name"] in index.text
    assert "[names only]" in index.text


def test_no_downgrade_when_budget_is_enough():
    entries = [_entry("pdf", "documents"), _entry("docx", "documents")]
    index = build_skill_index(entries, budget=12000)
    assert index.names_only == 0
    assert "  - pdf:" in index.text
    assert "[names only]" not in index.text


def test_platform_metadata_never_hides_names():
    """platforms 只作元数据：实测误藏过 python-debugpy 等 Windows 可用技能。"""
    entries = [
        _entry("pdf"),
        _entry("mac-only", group="other", platforms=["darwin"], platform_ok=False),
    ]
    index = build_skill_index(entries, budget=12000)
    assert index.visible == 2
    assert "pdf" in index.text
    assert "mac-only" in index.text


def test_order_and_signature_are_stable():
    entries = [
        _entry("z-last", "other"),
        _entry("a-first", "documents"),
        _entry("m-mid", "coding"),
    ]
    first = build_skill_index(entries, budget=12000)
    second = build_skill_index(list(reversed(entries)), budget=12000)
    assert first.signature == second.signature
    assert first.text == second.text
    # 文档类在编码类之前（按固定类目顺序）
    assert first.text.index("- documents:") < first.text.index("- coding:")


def test_signature_changes_when_content_changes():
    base = [_entry("pdf", desc="处理 PDF")]
    changed = [_entry("pdf", desc="处理 PDF 与 OCR")]
    assert (
        build_skill_index(base, budget=12000).signature
        != build_skill_index(changed, budget=12000).signature
    )


def test_when_to_use_takes_priority_over_description():
    entry = _entry(
        "humanizer",
        group="writing",
        desc="这是一个不太相关的描述。",
        when_to_use="润色/AI 味文本改写。",
    )
    index = build_skill_index([entry], budget=12000)
    assert "润色/AI 味文本改写" in index.text
    assert "不太相关的描述" not in index.text


def test_budget_change_changes_signature():
    entries = [_entry("pdf")]
    assert (
        build_skill_index(entries, budget=12000).signature
        != build_skill_index(entries, budget=8000).signature
    )
