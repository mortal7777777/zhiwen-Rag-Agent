"""数据访问层公共序列化/哈希辅助。"""

from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)


def _json_dumps(obj) -> str | None:
    return json.dumps(obj, ensure_ascii=False) if obj is not None else None


def _json_loads(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def content_hash(text: str) -> str:
    """提示词版本指纹：UTF-8 原字节 SHA-256（不做空白归一化）。"""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()
