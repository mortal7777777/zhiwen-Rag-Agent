"""技能 · 结构提取（从 app/skills.py 拆出，2026-10-10）。

SKILL.md 章节解析：skill_sections 返回全部章节（含字符区间，供按节读取），
skill_structure 在其上做优先级排序与截断（供索引/预览）。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PRIORITY_SECTIONS = (
    "when to use",
    "prerequisites",
    "steps",
    "how to",
    "usage",
    "examples",
    "workflow",
    "recommended",
    "gotchas",
    "troubleshooting",
)


def skill_sections(skill_md_path: str, max_sections: int | None = None) -> list[dict]:
    """按文档顺序解析章节：{heading, summary, start, end}。

    start/end 是正文（不含标题行）在原文中的字符区间，供 skill_view 按节读取。
    """
    try:
        text = Path(skill_md_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []

    sections: list[dict] = []
    current_heading = ""
    current_lines: list[str] = []
    current_start = 0
    offset = 0

    def flush(end_offset: int) -> None:
        if current_heading and current_lines:
            summary = " ".join(
                ln.strip() for ln in current_lines if ln.strip()
            ).strip()
            sections.append(
                {
                    "heading": current_heading,
                    "summary": summary[:300],
                    "start": current_start,
                    "end": end_offset,
                }
            )

    for line in text.splitlines(keepends=True):
        if line.startswith("#"):
            flush(offset)
            current_heading = line.lstrip("#").strip()
            current_start = offset + len(line)
            current_lines = []
        elif current_heading:
            current_lines.append(line)
        offset += len(line)
    flush(offset)

    cleaned = [s for s in sections if s["heading"] and s["heading"] != "---"]
    return cleaned[:max_sections] if max_sections else cleaned


def skill_structure(skill_md_path: str, max_sections: int = 8) -> list[dict]:
    """章节摘要（按优先级排序后截断），供索引/预览使用。"""
    sections = skill_sections(skill_md_path)
    if not sections:
        return []
    sections.sort(
        key=lambda s: (
            next(
                (
                    i
                    for i, key in enumerate(_PRIORITY_SECTIONS)
                    if key in s["heading"].lower()
                ),
                99,
            ),
            len(s["summary"]),
        )
    )
    return sections[:max_sections]
