"""技能 · 索引组装（从 app/skills.py 拆出，2026-10-10）。

build_skill_index：把启用技能按功能类目组装成 **全量** 名称索引（L0 渐进披露）。
名称永不隐藏；预算不足时按固定类目顺序把描述降级为 names-only。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass

from .sanitize import sanitize_skill_text
from .scan import SKILL_GROUPS

logger = logging.getLogger(__name__)

_DESC_LIMIT = 96

# 类目展示顺序（与 scan.SKILL_GROUPS 一致）
_GROUP_ORDER = list(SKILL_GROUPS)

# 预算不足时的降级顺序：先降低频/边缘类目，文档与编码类最后降
_DOWNGRADE_ORDER = [
    "other",
    "media",
    "email",
    "github",
    "data-ml",
    "productivity",
    "design",
    "research",
    "writing",
    "coding",
    "documents",
]

_INDEX_CACHE: dict[str, "SkillIndex"] = {}
_INDEX_CACHE_LOCK = threading.Lock()
_INDEX_CACHE_MAX = 8


@dataclass(frozen=True)
class SkillIndex:
    """L0 索引产物：文本 + 稳定签名 + 可观测统计。"""

    text: str
    signature: str
    visible: int
    names_only: int
    chars: int


def _group_rank(group: str) -> int:
    try:
        return _GROUP_ORDER.index(group)
    except ValueError:
        return len(_GROUP_ORDER)


def _brief(entry: dict) -> str:
    """一句话触发描述：优先 frontmatter when_to_use，否则 description 首句。"""
    raw = str(entry.get("when_to_use") or entry.get("description") or "")
    one = " ".join(raw.split())
    match = re.search(r"[。！？]|(?<=[.!?])\s+", one)
    if match and match.start() <= _DESC_LIMIT:
        one = one[: match.start()].strip()
    one = " ".join(sanitize_skill_text(one, 1000).split())
    if len(one) > _DESC_LIMIT:
        one = one[:_DESC_LIMIT].rstrip() + "…"
    return one


def _source_tag(entry: dict) -> str:
    source = str(entry.get("source") or "").strip()
    return f" [{source}]" if source else ""


def _grouped(entries: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        group = str(entry.get("group") or "other")
        groups.setdefault(group, []).append(entry)
    for items in groups.values():
        items.sort(key=lambda e: str(e.get("name") or ""))
    return groups


def _render(entries: list[dict], downgraded: set[str]) -> str:
    groups = _grouped(entries)
    lines = ["<available_skills>"]
    for group in sorted(groups, key=lambda g: (_group_rank(g), g)):
        items = groups[group]
        if group in downgraded:
            names = ", ".join(str(e.get("name") or "") for e in items)
            lines.append(f"- {group} [names only]: {names}")
        else:
            lines.append(f"- {group}:")
            for entry in items:
                lines.append(
                    f"  - {entry.get('name')}: {_brief(entry)}{_source_tag(entry)}"
                )
    lines.append("</available_skills>")
    return "\n".join(lines)


def _signature(entries: list[dict], budget: int) -> str:
    payload = [
        {
            "name": e.get("name"),
            "brief": _brief(e),
            "source": e.get("source"),
            "group": e.get("group") or "other",
        }
        for e in entries
    ]
    raw = json.dumps(
        {"budget": budget, "entries": payload},
        ensure_ascii=False,
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _downgrade_order(groups: dict[str, list[dict]]) -> list[str]:
    ordered = [g for g in _DOWNGRADE_ORDER if g in groups]
    ordered.extend(sorted(g for g in groups if g not in ordered))
    return ordered


def build_skill_index(entries: list[dict], budget: int = 12000) -> SkillIndex:
    """构建 L0 全量索引。

    规则：**所有**技能名称都出现在输出中；超预算时整类目降级为 names-only
    （名称仍保留），绝不静默隐藏。`platforms` 只作为元数据记录，不据此过滤
    ——实测 Windows 上会误藏 python-debugpy / research-paper-writing 等可用技能。
    签名只由影响输出的字段决定，供 prepare 做模块级缓存与"索引变化"日志。
    """
    candidates = list(entries)
    candidates.sort(
        key=lambda e: (
            _group_rank(str(e.get("group") or "other")),
            str(e.get("name") or ""),
        )
    )
    signature = _signature(candidates, budget)
    with _INDEX_CACHE_LOCK:
        cached = _INDEX_CACHE.get(signature)
    if cached is not None:
        return cached

    downgraded: set[str] = set()
    text = _render(candidates, downgraded)
    if len(text) > budget:
        groups = _grouped(candidates)
        for group in _downgrade_order(groups):
            downgraded.add(group)
            text = _render(candidates, downgraded)
            if len(text) <= budget:
                break
    if len(text) > budget:
        logger.warning(
            "技能索引仍超出预算：%d > %d 字符（名称不允许隐藏，已全量输出）",
            len(text),
            budget,
        )

    groups = _grouped(candidates)
    index = SkillIndex(
        text=text,
        signature=signature,
        visible=len(candidates),
        names_only=sum(len(groups[g]) for g in downgraded),
        chars=len(text),
    )
    with _INDEX_CACHE_LOCK:
        if len(_INDEX_CACHE) >= _INDEX_CACHE_MAX:
            _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
        _INDEX_CACHE[signature] = index
    return index
