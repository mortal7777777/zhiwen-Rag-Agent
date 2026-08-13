"""编排层纯逻辑单测：子代理拆分 / 来源重编号 / 计划硬约束判断。"""

from __future__ import annotations

from app.agent.langgraph_agent import (
    _build_subagent_tasks,
    _plan_hint,
    _remaining_needs_tools,
    _renumber_subagent_sources,
)


def test_build_subagent_tasks_filters_and_dedups():
    state = {
        "plan_map": [
            {"step": "检索知识库资料", "tool_hint": "knowledge_base_search"},
            {"step": "联网搜索最新信息", "tool_hint": "web_search"},
            {"step": "推理总结", "tool_hint": ""},
            {"step": "检索知识库资料", "tool_hint": "knowledge_base_search"},
        ]
    }
    tasks = _build_subagent_tasks(state)
    assert [t["step"] for t in tasks] == ["检索知识库资料", "联网搜索最新信息"]


def test_build_subagent_tasks_cap_four():
    state = {
        "plan_map": [
            {"step": f"步骤{i}", "tool_hint": "web_search"} for i in range(6)
        ]
    }
    assert len(_build_subagent_tasks(state)) == 4


def test_plan_hint_mapping():
    assert _plan_hint("检索知识库中的文档")["tool_hint"] == "knowledge_base_search"
    assert _plan_hint("联网搜索最新新闻")["tool_hint"] == "web_search"
    assert _plan_hint("写一个脚本并运行")["tool_hint"] == "file_tool/bash"
    assert _plan_hint("整理总结")["tool_hint"] == ""


def test_renumber_subagent_sources():
    counter = [5]
    results = [
        {
            "sources": [
                {"index": 1, "type": "kb", "content": "a"},
                {"index": 2, "type": "web", "content": "b"},
            ]
        },
        {"sources": [{"index": 1, "type": "kb", "content": "c"}]},
    ]
    merged = _renumber_subagent_sources(results, counter)
    assert [s["index"] for s in merged] == [6, 7, 8]
    assert counter[0] == 8


def test_remaining_needs_tools():
    assert _remaining_needs_tools(
        [{"text": "检索知识库", "tool_hint": "knowledge_base_search"}]
    )
    assert _remaining_needs_tools(
        [{"step": "运行脚本", "tool_hint": "file_tool/bash"}]
    )
    assert not _remaining_needs_tools([{"text": "整理总结"}])
