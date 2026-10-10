"""供应商预设表约束（app/provider_presets.py）。

预设是"选错端点/格式/模型名"事故的第一道防线（2026-10-10 接入 OpenCode Go
实测踩坑：文档给的是完整端点地址，粘贴进 base_url 后 SDK 再拼一段 → 404）。
这里钉住字段约束与归一化幂等，防止条目腐化。
"""

from __future__ import annotations

import re

from app.provider_presets import PROVIDER_PRESETS
from app.runtime_config import normalize_base_url, provider_api_format

REQUIRED_FIELDS = {
    "key",
    "name",
    "type",
    "base_url",
    "api_format",
    "models",
    "thinking_enabled",
    "note",
    "docs_url",
    "verified_at",
}


def test_presets_have_required_fields():
    assert PROVIDER_PRESETS, "预设表不能为空"
    for p in PROVIDER_PRESETS:
        missing = REQUIRED_FIELDS - set(p)
        assert not missing, f"{p.get('key')} 缺少字段 {missing}"
        assert p["type"] in ("chat", "vision"), p["key"]
        assert p["api_format"] in ("", "openai", "anthropic"), p["key"]
        assert isinstance(p["models"], list) and all(
            isinstance(m, str) and m.strip() for m in p["models"]
        ), p["key"]
        assert isinstance(p["thinking_enabled"], bool), p["key"]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", p["verified_at"]), p["key"]


def test_preset_keys_unique():
    keys = [p["key"] for p in PROVIDER_PRESETS]
    assert len(keys) == len(set(keys))


def test_preset_base_url_is_canonical():
    """base_url 必须是 SDK 规范 base：归一化幂等 + 格式判定与声明一致。"""
    for p in PROVIDER_PRESETS:
        fmt = p["api_format"] or ("anthropic" if "/anthropic" in p["base_url"] else "openai")
        assert normalize_base_url(p["base_url"], fmt) == p["base_url"], p["key"]
        assert provider_api_format(p) == fmt, p["key"]
