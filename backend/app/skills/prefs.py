"""技能 · 启停/隐藏偏好（从 app/skills.py 拆出，2026-10-10）。

带进程内缓存与锁的 pref 读写（enabled/hidden），供扫描与 API 使用。"""


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

DEFAULT_ENABLED = {
    # 文档与知识库处理
    "pdf", "ocr-and-documents", "docx", "xlsx", "powerpoint", "nano-pdf",
    # 写作与内容
    "content-research-writer", "email-draft-polish", "humanizer",
    "tailored-resume-generator", "meeting-notes-and-actions",
    # 研究与资料
    "arxiv", "research-paper-writing", "grounded-citations",
    # 个人效率
    "file-organizer",
    # 前端/设计
    "design-taste-frontend", "canvas-design", "architecture-diagram",
    "baoyu-infographic",
    # 编程与调试
    "create-plan", "systematic-debugging", "simplify-code",
    "test-driven-development", "requesting-code-review", "codebase-inspection",
    # 数据与机器学习
    "jupyter-live-kernel",
}


_prefs: dict | None = None


_prefs_lock = threading.Lock()


def _get_cached_prefs() -> dict:
    with _prefs_lock:
        if _prefs is None:
            return {
                "enabled": set(DEFAULT_ENABLED),
                "hidden": set(),
                "custom": False,
            }
        return {
            "enabled": set(_prefs.get("enabled") or []),
            "hidden": set(_prefs.get("hidden") or []),
            "custom": bool(_prefs.get("custom")),
        }


def load_prefs(db, force: bool = False) -> dict:
    """从数据库读取技能偏好；无自定义记录时使用默认精选手集。"""
    global _prefs
    if db is None:
        return _get_cached_prefs()
    raw = None
    try:
        from ..db import repository as repo

        raw = repo.get_meta(db, "skill_prefs")
    except Exception as exc:
        logger.warning("读取技能偏好失败：%s", exc)
    data: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except Exception as exc:
            logger.warning("skill_prefs 解析失败：%s", exc)
    with _prefs_lock:
        if data.get("custom"):
            _prefs = {
                "enabled": set(data.get("enabled") or []),
                "hidden": set(data.get("hidden") or []),
                "custom": True,
            }
        else:
            _prefs = {
                "enabled": set(DEFAULT_ENABLED),
                "hidden": set(),
                "custom": False,
            }
    return _get_cached_prefs()


def save_prefs(db, enabled: list[str] | None = None, hidden: list[str] | None = None) -> dict:
    """保存技能偏好并立即生效。enabled/hidden 传空列表即清空。"""
    global _prefs
    prev = _get_cached_prefs()
    new_enabled = set(str(x).strip() for x in (enabled if enabled is not None else prev["enabled"]) if x)
    new_hidden = set(str(x).strip() for x in (hidden if hidden is not None else prev["hidden"]) if x)
    # 隐藏的技能不应同时处于启用状态
    new_enabled -= new_hidden
    data = {
        "enabled": sorted(new_enabled),
        "hidden": sorted(new_hidden),
        "custom": True,
    }
    with _prefs_lock:
        _prefs = {
            "enabled": new_enabled,
            "hidden": new_hidden,
            "custom": True,
        }
    if db is not None:
        try:
            from ..db import repository as repo

            repo.set_meta(db, "skill_prefs", json.dumps(data, ensure_ascii=False))
        except Exception as exc:
            logger.warning("保存技能偏好失败：%s", exc)
    return _get_cached_prefs()


def reset_prefs(db) -> dict:
    """恢复默认精选手集。"""
    return save_prefs(db, enabled=sorted(DEFAULT_ENABLED), hidden=[])
