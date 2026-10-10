"""技能 · 自创技能管理（从 app/skills.py 拆出，2026-10-10）。

Agent 创建技能的定位/共享/取消共享/删除/内容读取。"""


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

from . import scan as scan_mod  # 模块引用而非值导入：测试 monkeypatch S.scan.AGENT_SKILLS_DIR 才能穿透

_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")


def _resolve_agent_skill(dir_name: str, category: str | None = None) -> Path | None:
    """定位自创技能目录并防路径穿越；返回绝对路径或 None。"""
    dir_name = (dir_name or "").strip()
    if not _SAFE_NAME_RE.fullmatch(dir_name):
        return None
    cat = (category or "").strip()
    if cat and not _SAFE_NAME_RE.fullmatch(cat):
        cat = ""
    root = Path(scan_mod.AGENT_SKILLS_DIR)
    if not root.is_dir():
        return None
    candidates: list[Path] = (
        [root / cat / dir_name] if cat else sorted(root.glob(f"*/{dir_name}"))
    )
    for cand in candidates:
        try:
            resolved = cand.resolve()
            resolved.relative_to(root.resolve())
        except (ValueError, OSError):
            continue
        if (resolved / "SKILL.md").is_file():
            return resolved
    return None


def _read_skill_origin(skill_dir: Path) -> str:
    """读取技能 frontmatter 的 origin 字段（判断是否为本助手创建/共享）。"""
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="ignore")[:800]
    except OSError:
        return ""
    _, _, _, extra = scan_mod._parse_frontmatter(text)
    return str(extra.get("origin") or "")


def share_agent_skill(dir_name: str, category: str | None = None) -> dict:
    """把自创技能复制到 Hermes 个人技能层（共享给 Hermes 使用）。"""
    src = _resolve_agent_skill(dir_name, category)
    if src is None:
        raise FileNotFoundError(f"未找到自创技能 {dir_name}")
    rel = src.relative_to(Path(scan_mod.AGENT_SKILLS_DIR).resolve())
    dst = scan_mod.HERMES_SKILLS_DIR / rel
    updated = (dst / "SKILL.md").exists()
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    scan_mod.scan_skills(force=True)
    return {"ok": True, "updated": updated, "target": str(dst)}


def unshare_agent_skill(dir_name: str, category: str | None = None) -> dict:
    """取消共享：删除 Hermes 个人层里的副本（仅限 origin=agent 的目录）。"""
    src = _resolve_agent_skill(dir_name, category)
    if src is None:
        raise FileNotFoundError(f"未找到自创技能 {dir_name}")
    rel = src.relative_to(Path(scan_mod.AGENT_SKILLS_DIR).resolve())
    dst = scan_mod.HERMES_SKILLS_DIR / rel
    if not (dst / "SKILL.md").is_file():
        return {"ok": True, "updated": False}
    if _read_skill_origin(dst) != scan_mod.AGENT_SOURCE:
        raise PermissionError("该目录不是本助手共享的副本，拒绝删除")
    shutil.rmtree(dst)
    scan_mod.scan_skills(force=True)
    return {"ok": True, "updated": True, "target": str(dst)}


def delete_agent_skill(dir_name: str, category: str | None = None) -> dict:
    """删除自创技能；已共享的副本一并清理（仅限 origin=agent 的目录）。"""
    src = _resolve_agent_skill(dir_name, category)
    if src is None:
        raise FileNotFoundError(f"未找到自创技能 {dir_name}")
    rel = src.relative_to(Path(scan_mod.AGENT_SKILLS_DIR).resolve())
    dst = scan_mod.HERMES_SKILLS_DIR / rel
    removed_shared = False
    if (dst / "SKILL.md").is_file() and _read_skill_origin(dst) == scan_mod.AGENT_SOURCE:
        shutil.rmtree(dst)
        removed_shared = True
    shutil.rmtree(src)
    scan_mod.scan_skills(force=True)
    return {"ok": True, "removed_shared": removed_shared}


def agent_skill_content(
    dir_name: str, category: str | None = None, limit: int = 20000
) -> dict:
    """读取自创技能 SKILL.md 全文（预览用）。"""
    src = _resolve_agent_skill(dir_name, category)
    if src is None:
        raise FileNotFoundError(f"未找到自创技能 {dir_name}")
    text = (src / "SKILL.md").read_text(encoding="utf-8", errors="ignore")
    return {
        "ok": True,
        "name": dir_name,
        "content": text[:limit],
        "truncated": len(text) > limit,
    }
