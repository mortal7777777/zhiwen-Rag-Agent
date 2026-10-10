"""技能 · 结构提取（从 app/skills.py 拆出，2026-10-10）。

SKILL.md 章节摘要（skill_structure，供注入与预览）。"""


from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
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


def skill_structure(skill_md_path: str, max_sections: int = 8) -> list[dict]:
    """把 SKILL.md 解析成 {heading, summary} 段目录，供模型按需展开。

    相比"前 N 字符截断"，分节目录能保留关键段（When to Use / Prerequisites /
    Steps）的完整入口，避免复杂技能的关键步骤被截掉。
    """
    try:
        text = Path(skill_md_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    sections: list[dict] = []
    current_heading = ""
    current_lines: list[str] = []

    def flush():
        if current_heading and current_lines:
            summary = " ".join(
                ln.strip() for ln in current_lines if ln.strip()
            ).strip()
            sections.append(
                {
                    "heading": current_heading,
                    "summary": summary[:300],
                }
            )

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            current_heading = line.lstrip("#").strip()
            current_lines = []
        elif current_heading:
            current_lines.append(line)
    flush()

    if not sections:
        return []
    # 去掉 frontmatter 伪段落（--- 之类），并按优先级排序
    cleaned = [
        s for s in sections if s["heading"] and s["heading"] != "---"
    ]
    cleaned.sort(
        key=lambda s: (
            next(
                (i for i, key in enumerate(_PRIORITY_SECTIONS) if key in s["heading"].lower()),
                99,
            ),
            len(s["summary"]),
        )
    )
    return cleaned[:max_sections]
