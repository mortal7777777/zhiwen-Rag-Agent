"""MCP 配置脱敏：API 只回显掩码，保存时按掩码保留数据库里的真实值。

背景：GET /api/mcp 原样返回 env/headers，Authorization、API Key 等令牌
会出现在接口响应与浏览器开发者工具里；PUT 又把前端回传的值整体覆盖。
这里统一做「读取脱敏 + 写回保留」。
"""

from __future__ import annotations

import re

SECRET_MASK = "__MASKED__"

_SECRET_KEY_RE = re.compile(
    r"(authorization|api[-_]?key|token|secret|password|passwd|cookie|credential|auth)",
    re.I,
)


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(str(key or "")))


def mask_mcp_servers(servers: list[dict]) -> list[dict]:
    """返回脱敏副本：env/headers 中敏感键的值替换为 SECRET_MASK。"""
    masked: list[dict] = []
    for server in servers or []:
        item = dict(server)
        for field in ("env", "headers"):
            values = item.get(field)
            if isinstance(values, dict):
                item[field] = {
                    key: (
                        SECRET_MASK
                        if is_secret_key(key) and str(value).strip()
                        else value
                    )
                    for key, value in values.items()
                }
        masked.append(item)
    return masked


def merge_mcp_secrets(incoming: list[dict], existing: list[dict]) -> list[dict]:
    """保存前合并：值为 SECRET_MASK 的键沿用旧值；缺省的键视为删除。"""
    old_by_id = {str(server.get("id") or ""): server for server in existing or []}
    merged_servers: list[dict] = []
    for server in incoming or []:
        item = dict(server)
        old = old_by_id.get(str(item.get("id") or "")) or {}
        for field in ("env", "headers"):
            merged: dict[str, str] = {}
            for key, value in (item.get(field) or {}).items():
                if str(value) == SECRET_MASK:
                    old_value = (old.get(field) or {}).get(key)
                    if old_value is not None:
                        merged[key] = old_value
                else:
                    merged[key] = value
            item[field] = merged
        merged_servers.append(item)
    return merged_servers
