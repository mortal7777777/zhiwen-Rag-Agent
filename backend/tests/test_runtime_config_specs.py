"""可调项声明表（P1-2）：单一来源的键集合快照 + 派生校验 + 保存清洗。

背景：可调项原先散在三处（EDITABLE_KEYS / _INT_RANGES+_FLOAT_RANGES / _BOOL_KEYS），
新增一项要"三处同步"，漏一处就是"设置项静默不可改"。现在全部由 _SPECS 派生，
本文件用快照锁住键集合，防止重构中丢键。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import runtime_config as rc

# 2026-10-08 重构前的键集合快照（65 个）——只能增，不能减
EXPECTED_EDITABLE_KEYS = {
    # 模型 / 供应商（旧的 deepseek_*/sensenova_* 键为兼容保留）
    "deepseek_api_key", "deepseek_base_url", "deepseek_model", "agent_title_model",
    "chat_temperature", "main_model_vision", "data_dir",
    "sensenova_api_key", "sensenova_base_url", "sensenova_model",
    "providers", "active_chat_provider", "active_vision_provider",
    # 联网搜索
    "web_search_provider", "web_search_max_results", "tavily_api_key",
    "searxng_base_url", "searxng_engines",
    # 技能与工具
    "skills_enabled", "skills_index_max_chars", "skills_full_view_max_chars",
    "skills_view_dedup", "skills_usage_tracking", "skills_curator_stale_days",
    "advanced_tools_enabled",
    "agent_task_max_iterations", "agent_task_max_failures",
    "agent_max_iterations", "agent_max_failures",
    "agent_max_dispatch_rounds", "agent_llm_stall_timeout_s",
    "task_mode_detect", "agent_subagents_enabled", "agent_subagent_max_rounds",
    "agent_subagent_max_llm_calls",
    "plan_mode_allowed",
    # 检索与上下文 / 记忆
    "kb_chat_min_score", "crag_fallback_enabled", "crag_min_score",
    "vision_max_images", "history_max_messages", "history_max_tokens",
    "memory_auto_extract_min_chars", "project_memory_max_chars",
    "memory_consolidate_threshold", "memory_consolidate_interval_hours",
    "checkpoint_keep_per_thread",
    "verify_command", "verify_auto_detect", "verify_max_retries",
    # 受控执行与审批
    "tool_workspace", "command_allowlist", "command_timeout", "command_sandbox",
    "sandbox_image", "sandbox_workspace_readonly", "tool_permission_mode",
    "permission_timeout", "skill_sandbox_enabled", "reasoning_summary_enabled",
    "project_memory_file", "mcp_enabled", "trajectory_compress_enabled",
    # 嵌入 / 重排
    "embedding_provider", "embedding_api_base_url", "embedding_api_key",
    "embedding_api_model",
    "reranker_provider", "reranker_api_base_url", "reranker_api_key",
    "reranker_api_model",
}


def test_editable_keys_snapshot():
    assert rc.EDITABLE_KEYS == EXPECTED_EDITABLE_KEYS, (
        "可调项集合发生变化：新增项请同步本快照；"
        f"缺失={sorted(EXPECTED_EDITABLE_KEYS - rc.EDITABLE_KEYS)} "
        f"多余={sorted(rc.EDITABLE_KEYS - EXPECTED_EDITABLE_KEYS)}"
    )


def test_derived_maps_consistent():
    # 派生表必须覆盖且仅覆盖声明表里的对应类型
    assert set(rc._INT_RANGES) == {k for k, s in rc._SPECS.items() if s.kind == "int"}
    assert set(rc._FLOAT_RANGES) == {k for k, s in rc._SPECS.items() if s.kind == "float"}
    assert set(rc._BOOL_KEYS) == {k for k, s in rc._SPECS.items() if s.kind == "bool"}
    assert set(rc._ENUM_KEYS) == {k for k, s in rc._SPECS.items() if s.kind == "enum"}
    # 区间必须有序、枚举必须有取值
    for key, (lo, hi) in list(rc._INT_RANGES.items()) + list(rc._FLOAT_RANGES.items()):
        assert lo < hi, f"{key} 区间非法：{lo}..{hi}"
    for key, choices in rc._ENUM_KEYS.items():
        assert choices, f"{key} 枚举为空"
    # 类型化键必须是可调项的子集
    assert set(rc._SPECS) <= rc.EDITABLE_KEYS


def test_editable_schema_covers_all_keys_and_leaks_nothing():
    rows = rc.editable_schema()
    by_key = {r["key"]: r for r in rows}
    assert set(by_key) == rc.EDITABLE_KEYS
    for key, row in by_key.items():
        if key.endswith("api_key"):
            assert row["default"] is None, f"{key} 不应在 schema 里暴露默认密钥"
        assert row["kind"] in ("int", "float", "bool", "enum", "str")


def test_editable_schema_has_labels_for_typed_keys():
    """类型化键必须带展示名与分组（设置页据此渲染，新增项别忘了填）。"""
    for row in rc.editable_schema():
        if row["kind"] in ("int", "float", "bool", "enum"):
            assert row["label"], f"{row['key']} 缺少 label"
            assert row["group"] != "other", f"{row['key']} 缺少 group"


# ---------------- 保存清洗（声明表驱动的校验） ----------------


@pytest.fixture()
def clean_overrides(monkeypatch):
    """隔离 _overrides 全局态，并把 repo.set_meta 换成内存写入。"""
    monkeypatch.setattr(rc, "_overrides", {})
    saved: dict = {}
    monkeypatch.setattr(
        "app.db.repository.set_meta",
        lambda db, key, value: saved.__setitem__(key, value),
    )
    return saved


def _save(updates: dict) -> dict:
    return rc.save_overrides(db=None, updates=updates, settings=None)


def test_save_overrides_clamps_numbers(clean_overrides):
    merged = _save(
        {
            "agent_max_iterations": 9999,
            "agent_max_failures": 0,
            "kb_chat_min_score": 5.0,
            "command_timeout": 99999,
            "permission_timeout": 1,
            "history_max_tokens": "not-a-number",
        }
    )
    assert merged["agent_max_iterations"] == 200
    assert merged["agent_max_failures"] == 1
    assert merged["kb_chat_min_score"] == 1.0
    assert merged["command_timeout"] == 300
    assert merged["permission_timeout"] == 30
    assert "history_max_tokens" not in merged, "非数值应跳过该键，不影响其它键"


def test_save_overrides_rejects_bad_enum_and_keeps_good_one(clean_overrides):
    merged = _save(
        {
            "tool_permission_mode": "yolo",
            "command_sandbox": "docker",
            "web_search_provider": "bogus",
        }
    )
    assert "tool_permission_mode" not in merged
    assert merged["command_sandbox"] == "docker"
    assert "web_search_provider" not in merged


def test_save_overrides_coerces_bools(clean_overrides):
    merged = _save({"mcp_enabled": 0, "skills_enabled": "1"})
    assert merged["mcp_enabled"] is False
    assert merged["skills_enabled"] is True


def test_save_overrides_ignores_unknown_and_empty_secrets(clean_overrides):
    merged = _save({"not_a_key": 1, "deepseek_api_key": "   "})
    assert "not_a_key" not in merged
    assert "deepseek_api_key" not in merged  # 空串表示"不修改"


# ---------------- effective / effective_or 语义 ----------------


def test_effective_reads_settings_attr_not_shadowed(monkeypatch):
    """回归：旧实现里 effective(key, default) 会忽略 settings（env/config）值。"""
    monkeypatch.setattr(rc, "_overrides", {})
    settings = SimpleNamespace(advanced_tools_enabled=False, mcp_enabled=True)
    assert rc.effective(settings, "advanced_tools_enabled") is False
    assert rc.effective(settings, "mcp_enabled") is True
    assert rc.effective(settings, "missing_key") is None


def test_effective_or_falls_back_only_when_missing(monkeypatch):
    monkeypatch.setattr(rc, "_overrides", {})
    settings = SimpleNamespace(skills_enabled=None, mcp_enabled=True)
    assert rc.effective_or(settings, "skills_enabled", True) is True
    assert rc.effective_or(settings, "mcp_enabled", False) is True
    assert rc.effective_or(settings, "not_a_field", "ask") == "ask"


def test_db_override_wins_over_settings_and_fallback(monkeypatch):
    monkeypatch.setattr(rc, "_overrides", {"mcp_enabled": False})
    settings = SimpleNamespace(mcp_enabled=True)
    assert rc.effective(settings, "mcp_enabled") is False
    assert rc.effective_or(settings, "mcp_enabled", True) is False


def test_advanced_tools_guard_reads_default_true(monkeypatch):
    """回归：/api/advanced 的受控执行开关曾因 default=False 读不到默认 True。"""
    from app.config import Settings

    monkeypatch.setattr(rc, "_overrides", {})
    settings = Settings()  # 默认 advanced_tools_enabled=True
    assert rc.effective_or(settings, "advanced_tools_enabled", True) is True
