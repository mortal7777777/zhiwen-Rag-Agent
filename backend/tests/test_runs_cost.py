"""用量面板成本估算：按模型单价 + 峰谷时段 + USD→CNY 汇率。

旧实现写死 deepseek-chat 量级单价（input_miss=2.0 / input_hit=0.5 /
output=8.0），对 OpenCode Go 的 DeepSeek V4.1 Flash 会高估 7~167 倍。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.api import runs as runs_module

# 2026-10-19 是周一；北京 13:00 = UTC 05:00（Off-Peak）
MONDAY_OFF_PEAK = datetime(2026, 10, 19, 13, 0)
# 北京 10:00 = UTC 02:00（Peak：UTC 01:00-04:00）
MONDAY_PEAK = datetime(2026, 10, 19, 10, 0)
SATURDAY = datetime(2026, 10, 24, 10, 0)  # 2026-10-24 周六


def test_peak_window_detection():
    assert runs_module._is_peak(MONDAY_PEAK) is True
    assert runs_module._is_peak(MONDAY_OFF_PEAK) is False
    assert runs_module._is_peak(SATURDAY) is False
    assert runs_module._is_peak(None) is False


def test_off_peak_cost_for_deepseek_v41_flash():
    # 1M miss + 1M hit + 1M output = (0.15 + 0.003 + 0.60) USD
    cost = runs_module._estimate_cost(
        1_000_000, 1_000_000, 1_000_000,
        model="deepseek-v4.1-flash",
        created_at=MONDAY_OFF_PEAK,
        usd_cny=7.2,
    )
    assert cost == pytest.approx(0.753 * 7.2)


def test_peak_cost_is_double():
    off = runs_module._estimate_cost(
        1_000_000, 1_000_000, 1_000_000,
        "deepseek-v4.1-flash", MONDAY_OFF_PEAK, 7.2,
    )
    peak = runs_module._estimate_cost(
        1_000_000, 1_000_000, 1_000_000,
        "deepseek-v4.1-flash", MONDAY_PEAK, 7.2,
    )
    assert peak == pytest.approx(off * 2)


def test_model_suffix_is_normalized():
    """: [1m] 后缀只影响窗口推断，不应导致单价匹配失败。"""
    cost = runs_module._estimate_cost(
        1_000_000, 0, 0,
        model="deepseek-v4.1-flash[1m]",
        created_at=MONDAY_OFF_PEAK,
        usd_cny=7.2,
    )
    assert cost == pytest.approx(0.15 * 7.2)


def test_unknown_model_uses_conservative_default(monkeypatch):
    monkeypatch.setattr(runs_module, "_active_model", lambda: "")
    cost = runs_module._estimate_cost(
        1_000_000, 0, 0,
        model="some-unknown-model",
        created_at=MONDAY_OFF_PEAK,
        usd_cny=7.2,
    )
    assert cost == pytest.approx(0.27 * 7.2)


def test_history_without_model_falls_back_to_active(monkeypatch):
    """历史运行没有 model 字段：用当前激活供应商的型号近似。"""
    monkeypatch.setattr(runs_module, "_active_model", lambda: "deepseek-v4.1-flash")
    cost = runs_module._estimate_cost(
        1_000_000, 0, 0,
        model=None,
        created_at=MONDAY_OFF_PEAK,
        usd_cny=7.2,
    )
    assert cost == pytest.approx(0.15 * 7.2)
