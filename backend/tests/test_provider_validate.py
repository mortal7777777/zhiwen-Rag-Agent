"""供应商清洗与保存契约（`_validate_providers` / `save_overrides` providers 分支）。

背景（2026-10-10 供应商页 cc-switch 化，第 0 批前置修复）：该模块此前没有直接
单测，四个实测缺陷靠这组用例钉住：
1. `****` 掩码还原绝不能回退 env/config 里的 deepseek_api_key（否则配任意
   base_url 就能把环境里的真实 Key 发出去）；
2. 默认模型必须出现在候选列表里（不变量）；
3. 显式空列表 = 删除全部供应商（此前删完刷新又回来）；
4. legacy 键只在本次显式保存时同步，且按 id 匹配而不是"第一个同类"。
"""

from __future__ import annotations

import pytest

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


def _save(updates: dict, settings=None) -> dict:
    return rc.save_overrides(db=None, updates=updates, settings=settings)


def _provider(**overrides) -> dict:
    base = {
        "id": "p_1",
        "name": "测试供应商",
        "type": "chat",
        "base_url": "https://api.example.com",
        "api_key": "sk-plain",
        "model": "m1",
        "models": ["m1"],
        "enabled": True,
    }
    base.update(overrides)
    return base


def test_masked_key_restores_only_by_id(clean_overrides):
    _save({"providers": [_provider(api_key="sk-old")]})
    # 同 id 的掩码 → 还原旧值
    merged = _save({"providers": [_provider(api_key="****-old")]})
    assert merged["providers"][0]["api_key"] == "sk-old"

    # 新 id 的掩码 → 空，绝不回退到 settings 里的 deepseek_api_key
    class _FakeSettings:
        deepseek_api_key = "sk-from-env-DO-NOT-LEAK"

    merged = _save(
        {"providers": [_provider(id="p_2", api_key="****-new")]},
        settings=_FakeSettings(),
    )
    new_provider = next(p for p in merged["providers"] if p["id"] == "p_2")
    assert new_provider["api_key"] == ""


def test_model_forced_into_models(clean_overrides):
    merged = _save(
        {"providers": [_provider(model="m9", models=["m1", "m2"])]},
    )
    assert merged["providers"][0]["models"][0] == "m9"


def test_empty_providers_list_deletes_all(clean_overrides):
    _save({"providers": [_provider()]})
    merged = _save({"providers": []})
    assert merged["providers"] == []
    # 非列表（坏载荷）不写入，也不清空
    _save({"providers": [_provider()]})
    merged = _save({"providers": "junk"})
    assert len(merged["providers"]) == 1


def test_api_format_persists(clean_overrides):
    merged = _save({"providers": [_provider(api_format="anthropic")]})
    assert merged["providers"][0]["api_format"] == "anthropic"
    # 非法值不落库（保持按 base_url 推断）
    merged = _save({"providers": [_provider(api_format="grpc")]})
    assert "api_format" not in merged["providers"][0]


def test_legacy_sync_only_on_explicit_update(clean_overrides):
    _save(
        {
            "providers": [
                _provider(id="deepseek", api_key="sk-cur"),
                _provider(id="other"),
            ]
        }
    )
    rc._overrides["deepseek_api_key"] = "sk-env"

    # 保存无关键：不得拿旧值覆盖供应商
    merged = _save({"chat_temperature": 0.7})
    deepseek = next(p for p in merged["providers"] if p["id"] == "deepseek")
    other = next(p for p in merged["providers"] if p["id"] == "other")
    assert deepseek["api_key"] == "sk-cur"
    assert other["api_key"] == "sk-plain"

    # 显式保存 legacy 键：只同步到 id=deepseek，不碰第一个同类
    merged = _save({"deepseek_api_key": "sk-new"})
    deepseek = next(p for p in merged["providers"] if p["id"] == "deepseek")
    other = next(p for p in merged["providers"] if p["id"] == "other")
    assert deepseek["api_key"] == "sk-new"
    assert other["api_key"] == "sk-plain"


def test_advanced_params_cleaned(clean_overrides):
    merged = _save(
        {
            "providers": [
                _provider(
                    temperature=0.0,
                    max_tokens=100,
                    headers={
                        "X-Trace": "  t1  ",
                        "Host": "evil.example",
                        "Authorization": "Bearer x",
                        "X-Bad Header": "v",
                        "X-Inj": "a\nb",
                        "X-Long": "a" * 3000,
                    },
                )
            ]
        }
    )
    p = merged["providers"][0]
    assert p["temperature"] == 0.0  # 0.0 必须保留（None 检查而非 or）
    assert p["max_tokens"] == 256  # 低于下限夹取
    # 黑名单/非法名/换行值/超长值全部丢弃，正常值 trim 后保留
    assert p["headers"] == {"X-Trace": "t1"}


def test_advanced_params_absent_when_empty(clean_overrides):
    merged = _save(
        {"providers": [_provider(temperature=None, max_tokens="", headers={})]}
    )
    p = merged["providers"][0]
    assert "temperature" not in p
    assert "max_tokens" not in p
    assert "headers" not in p
