"""供应商配置导入导出（设置页「导出 / 导入」）。

导出默认**不含密钥**（api_key 整个省略、敏感请求头按 mask_key 规则脱敏）；
include_keys=1 才带明文，属于"服务端已存明文的搬运"，前端必须二次确认。

导入复用 `_validate_providers` 做清洗，并叠加两条合并规则：
- 同 id 覆盖时，导入文件的空 Key / 掩码 Key = "保留旧值"（导入文件通常不含明文）；
- 新 id 的掩码 Key 一律拒绝（没有旧值可还原，防止把 **** 字面量发给上游）。
"""

from __future__ import annotations

from datetime import datetime

from .config import get_settings
from .runtime_config import (
    _validate_providers,
    get_providers,
    is_sensitive_header,
    mask_key,
    save_overrides,
)


def _mask_headers(headers) -> dict:
    if not isinstance(headers, dict):
        return headers
    return {
        name: (mask_key(val) or "" if is_sensitive_header(name) else val)
        for name, val in headers.items()
    }


def export_payload(include_keys: bool = False) -> dict:
    """生成导出 JSON（时间戳带时区，便于跨机器导入时判断新旧）。"""
    providers = get_providers(get_settings())
    out: list[dict] = []
    for provider in providers:
        item = dict(provider)
        if not include_keys:
            item.pop("api_key", None)
            if "headers" in item:
                item["headers"] = _mask_headers(item.get("headers"))
        out.append(item)
    return {
        "schema": 1,
        "app": "zhiwen",
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "include_keys": bool(include_keys),
        "providers": out,
    }


def import_providers(payload: dict, db=None, settings=None) -> dict:
    """合并导入：同 id 覆盖（空/掩码 Key 保留旧值），新 id 追加。"""
    raw = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return {"ok": False, "error": "文件缺少 providers 数组"}

    settings = settings or get_settings()
    existing = list(get_providers(settings))
    by_id = {p.get("id"): p for p in existing}
    skipped: list[dict] = []
    imported = updated = 0

    for entry in raw:
        if not isinstance(entry, dict):
            skipped.append({"id": "", "reason": "非法条目（非对象）"})
            continue
        pid = str(entry.get("id") or "").strip()
        if not pid:
            skipped.append({"id": "", "reason": "缺少 id"})
            continue
        old = by_id.get(pid)
        merged = dict(old) if old else {}
        merged.update(entry)
        key = str(entry.get("api_key") or "").strip()
        if old and (not key or key.startswith("****")):
            merged["api_key"] = old.get("api_key") or ""
        elif not old and key.startswith("****"):
            merged["api_key"] = ""
            skipped.append({"id": pid, "reason": "掩码 Key 无法用于新供应商，已置空"})
        if old:
            updated += 1
        else:
            imported += 1
        by_id[pid] = merged

    merged_list = _validate_providers(list(by_id.values()), settings=settings)
    # 端点始终带 db；测试把 repo.set_meta 换成内存写入（同 test_provider_validate 模式）
    save_overrides(db, {"providers": merged_list}, settings=settings)
    return {
        "ok": True,
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "total": len(merged_list),
    }
