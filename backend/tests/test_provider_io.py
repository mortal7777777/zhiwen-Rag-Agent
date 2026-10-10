"""供应商导入导出契约（provider_io）：脱敏、合并规则与拒绝边界。"""

from __future__ import annotations

import pytest

from app import provider_io as io
from app import runtime_config as rc


@pytest.fixture()
def clean_overrides(monkeypatch):
    monkeypatch.setattr(rc, "_overrides", {})
    saved: dict = {}
    monkeypatch.setattr(
        "app.db.repository.set_meta",
        lambda db, key, value: saved.__setitem__(key, value),
    )
    return saved


def _seed(providers: list[dict]) -> None:
    rc.save_overrides(db=None, updates={"providers": providers}, settings=None)


def _provider(**over):
    base = {
        "id": "p_1",
        "name": "测试供应商",
        "type": "chat",
        "base_url": "https://api.example.com",
        "api_key": "sk-old-1234",
        "model": "m1",
        "models": ["m1"],
        "enabled": True,
        "headers": {"X-Auth-Token": "secret-token-9876", "X-Trace": "t"},
    }
    base.update(over)
    return base


def test_export_without_keys_masks_secrets(clean_overrides):
    _seed([_provider()])
    payload = io.export_payload(include_keys=False)
    assert payload["schema"] == 1
    assert payload["include_keys"] is False
    assert "+" in payload["exported_at"] or payload["exported_at"].endswith("Z")
    item = payload["providers"][0]
    assert "api_key" not in item
    # 敏感头值脱敏为 ****last4；普通头原样
    assert item["headers"]["X-Auth-Token"] == "****9876"
    assert item["headers"]["X-Trace"] == "t"


def test_export_with_keys_keeps_plain(clean_overrides):
    _seed([_provider()])
    payload = io.export_payload(include_keys=True)
    assert payload["providers"][0]["api_key"] == "sk-old-1234"


def test_import_merge_rules(clean_overrides):
    _seed([_provider()])
    result = io.import_providers(
        {
            "providers": [
                # 同 id：空 Key 保留旧值，覆盖名称
                {"id": "p_1", "name": "改名", "type": "chat", "base_url": "https://api.example.com", "api_key": "", "model": "m1", "models": ["m1"]},
                # 新 id：明文 Key 正常写入
                {"id": "p_2", "name": "新增", "type": "chat", "base_url": "https://b.example.com", "api_key": "sk-new", "model": "m2", "models": ["m2"]},
                # 新 id：掩码 Key 置空并记录
                {"id": "p_3", "name": "掩码", "type": "chat", "base_url": "https://c.example.com", "api_key": "****abcd", "model": "m3", "models": ["m3"]},
                # 不合格条目
                {"name": "没有 id"},
                "不是对象",
            ]
        }
    )
    assert result["ok"] is True
    assert result["imported"] == 2 and result["updated"] == 1
    assert {s["reason"] for s in result["skipped"]} == {
        "掩码 Key 无法用于新供应商，已置空",
        "缺少 id",
        "非法条目（非对象）",
    }
    merged = {p["id"]: p for p in rc.get_overrides()["providers"]}
    assert merged["p_1"]["name"] == "改名"
    assert merged["p_1"]["api_key"] == "sk-old-1234"  # 空 Key 保留旧值
    assert merged["p_2"]["api_key"] == "sk-new"
    assert merged["p_3"]["api_key"] == ""  # 掩码置空，绝不外发字面量


def test_import_masked_key_on_existing_keeps_old(clean_overrides):
    _seed([_provider()])
    result = io.import_providers(
        {"providers": [{"id": "p_1", "type": "chat", "base_url": "https://api.example.com", "api_key": "****1234", "model": "m1", "models": ["m1"]}]}
    )
    assert result["ok"] is True and result["updated"] == 1
    merged = {p["id"]: p for p in rc.get_overrides()["providers"]}
    assert merged["p_1"]["api_key"] == "sk-old-1234"


def test_import_rejects_malformed_payload(clean_overrides):
    assert io.import_providers({})["ok"] is False
    assert io.import_providers({"providers": "junk"})["ok"] is False
