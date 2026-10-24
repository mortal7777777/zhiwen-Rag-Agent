"""工具面契约（P1-4 / P2-1）：同配置下工具集必须字节稳定，且不能悄悄变大。

`bind_tools` 的 tools 数组（名称+描述+schema，有序）是 provider 前缀缓存键的
一部分：同一会话内顺序或内容变化 = 整条缓存失效（实测命中率从 90% 掉到 29%）。
所以这里锁两件事：
1. 同配置重复构建 → 工具名序列与 `_tools_prefix_hash` 完全一致；
2. 开关切换（计划模式/子代理/受控执行）→ 必然变化（说明开关真的生效）；
3. 工具面总字符数有预算上限（防日后无意识膨胀把每次请求撑大）。
"""

from __future__ import annotations

import json

import pytest

from app import runtime_config
from app.agent.langgraph_agent import LangGraphAgentService
from app.agent.utils import _tools_prefix_hash, _tools_prefix_size
from app.config import Settings

# 工具面预算（字符）：2026-10-08 压缩前 5,747（含技能/视觉时约 7.3k），
# 压缩后 5,155 → 上限 5,400（约 4.5% 余量）。要加工具请先压缩或改按需注册。
TOOLS_CHARS_BUDGET = 5400


class _FakeRag:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def acquire_gpu(self) -> None: ...

    def release_gpu(self) -> None: ...

    def retrieve(self, question, history=None):
        return []


def _service(monkeypatch, **overrides) -> LangGraphAgentService:
    # 隔离全局覆盖态 + 不扫真实技能/用户工具目录（保持用例可重复、无外部依赖）
    monkeypatch.setattr(runtime_config, "_overrides", dict(overrides))
    monkeypatch.setattr("app.skills.scan_skills", lambda *a, **k: [])
    monkeypatch.setattr("app.user_tool_loader.load_user_tools", lambda: [])
    settings = Settings()
    return LangGraphAgentService(settings, _FakeRag(settings))


def _build(svc: LangGraphAgentService) -> list:
    return svc.build_tool_list(
        None,
        use_web_search=True,
        use_knowledge_base=True,
        conv_id=None,
        project_dir=None,
        command_sandbox=None,
    )


def test_tool_surface_is_byte_stable_for_same_config(monkeypatch):
    svc = _service(monkeypatch)
    first = _build(svc)
    second = _build(svc)
    assert [t.name for t in first] == [t.name for t in second]
    assert _tools_prefix_hash(first) == _tools_prefix_hash(second)
    assert len(first) >= 10, "默认工具面不应为空（子代理/计划/受控执行默认开）"


def test_plan_mode_switch_changes_tool_surface(monkeypatch):
    on = _build(_service(monkeypatch, plan_mode_allowed=True))
    off = _build(_service(monkeypatch, plan_mode_allowed=False))
    assert "exit_plan_mode" in {t.name for t in on}
    assert "exit_plan_mode" not in {t.name for t in off}
    assert _tools_prefix_hash(on) != _tools_prefix_hash(off)


def test_subagent_and_advanced_tools_switches(monkeypatch):
    base = {t.name for t in _build(_service(monkeypatch))}
    assert "dispatch_subtasks" in base
    assert "write_file" in base

    no_sub = {
        t.name
        for t in _build(_service(monkeypatch, agent_subagents_enabled=False))
    }
    assert "dispatch_subtasks" not in no_sub

    no_tools = {
        t.name
        for t in _build(_service(monkeypatch, advanced_tools_enabled=False))
    }
    assert "write_file" not in no_tools


def test_tool_surface_stays_within_char_budget(monkeypatch):
    """预算守门：工具面膨胀会同时抬高每轮请求体积与首次缓存成本。"""
    tools = _build(_service(monkeypatch))
    size = _tools_prefix_size(tools)
    assert size <= TOOLS_CHARS_BUDGET, (
        f"工具面 {size} 字符，超过预算 {TOOLS_CHARS_BUDGET}："
        "请压缩描述/schema，或按需注册低频工具后再调高预算（并在提交说明里写明原因）"
    )


def test_tools_breakdown_splits_mcp():
    from types import SimpleNamespace

    from app.agent.utils import _tools_breakdown

    tools = [
        SimpleNamespace(name="core_tool", description="c", args_schema=None),
        SimpleNamespace(name="mcp_remote", description="m", args_schema=None),
    ]
    breakdown = _tools_breakdown(tools)
    assert breakdown["count"] == 2
    assert breakdown["core_count"] == 1
    assert breakdown["mcp_count"] == 1
    assert breakdown["mcp_chars"] > 0
    assert breakdown["chars"] >= breakdown["core_chars"] + breakdown["mcp_chars"]


def test_tool_descriptions_have_no_duplicated_boilerplate(monkeypatch):
    """描述里的"引用编号"约定应在系统提示里讲一次，而不是每个工具重复一遍。"""
    tools = _build(_service(monkeypatch))
    repeated = [
        t.name
        for t in tools
        if t.description and t.description.count("回答引用时用 [n] 标注") > 0
    ]
    assert len(repeated) <= 1, f"以下工具重复了引用编号说明：{repeated}"
