"""技能 · 目录组装（从 app/skills.py 拆出，2026-10-10）。

build_skill_catalog 与 matching_skills_for_injection：把启用技能
按需组装进上下文（跨 scan/structure/sanitize 三个域的编排）。"""


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

from .sanitize import sanitize_skill_text
from .scan import scan_skills, search_skills
from .structure import skill_structure

def build_skill_catalog(enabled_ids: set[str] | None = None, limit: int = 40) -> str:
    """生成紧凑的技能目录文本（名称 + 一句话描述），注入系统提示词。"""
    skills = scan_skills()
    if enabled_ids is not None:
        skills = [s for s in skills if s["name"] in enabled_ids]
    lines = []
    for s in skills[:limit]:
        desc = " ".join((s.get("description") or "").split())[:90]
        lines.append(f"- {s['name']}：{desc}")
    return "\n".join(lines)


def matching_skills_for_injection(
    query: str,
    enabled_ids: set[str] | None = None,
    top_k: int = 2,
    max_sections: int = 6,
) -> str:
    """按关键词匹配当前问题最相关的技能，返回清洗后的结构化说明。

    用于 prepare 阶段自动注入（不等模型主动调用 skill_lookup），
    内容经 sanitize_skill_text 过滤高风险指令。
    """
    hits = search_skills(
        query,
        embeddings=None,
        top_k=top_k,
        enabled_ids=enabled_ids,
    )
    blocks = []
    for h in hits:
        sections = skill_structure(h["path"], max_sections=max_sections)
        sec_text = "\n".join(
            f"- {s['heading']}：{sanitize_skill_text(s['summary'], 200)}"
            for s in sections
        )
        blocks.append(
            f"### {h['name']}（来源：{h['source']}）\n"
            f"{sanitize_skill_text(h.get('description') or '', 200)}\n"
            f"{sec_text}"
        )
    return "\n\n".join(blocks)
