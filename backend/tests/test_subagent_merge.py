"""merge 节点单测：子任务结果按 tool_call 回填成 ToolMessage。

两条要点：
1. 只消费本轮新增结果——AgentState 经 checkpointer 按 conversation 持久化，
   subagent_results 的归约器是 operator.add（只增不减），全量遍历会把历史
   轮次的结论重拼一遍（实测 4 轮后单行累积 3,870 字符，整份都是缓存 miss）。
2. 结果以 **ToolMessage**（工具返回值）进入主 agent 上下文，而不是 system
   消息断言"（已完成）"——2026-09-24 事故正是 system 断言放大了子代理的
   错误结论（它把磁盘上的历史遗留产物当成成果上报）。
"""

from __future__ import annotations

import json
from queue import Queue

from langchain_core.messages import ToolMessage

from app.agent.nodes.subagent import DISPATCH_TOOL_NAME, _merge_node
from app.agent.state import EventBus


def _result(task: str, call_id: str = "c1") -> dict:
    return {
        "name": task[:40],
        "task": task,
        "mode": "research",
        "call_id": call_id,
        "summary": f"{task} 的结论",
        "artifacts": [
            {"path": "a.html", "action": "created", "bytes": 10, "lines": 2}
        ],
        "commands": [{"command": "python -c ...", "exit_code": 0}],
        "files_read": ["b.txt"],
        "verify": [{"path": "a.html", "command": "node --check", "exit_code": 0}],
        "sources": [],
        "tool_trace": [],
    }


def _state(results: list[dict], consumed: int) -> dict:
    return {
        "bus": EventBus(Queue()),
        "db": None,
        "runtime": {},
        "messages": [],
        "subagent_results": results,
        "counter": [0],
        "tool_trace": [],
        "sources": [],
        "todos": [],
        "subagent_consumed": consumed,
        "dispatch_rounds": 0,
    }


def _tool_rows(messages: list) -> list[ToolMessage]:
    return [m for m in messages if isinstance(m, ToolMessage)]


def test_merge_emits_tool_messages_with_evidence():
    out = _merge_node(
        _state([_result("任务A", "c1"), _result("任务B", "c2")], consumed=0)
    )
    rows = _tool_rows(out["messages"])
    assert [r.tool_call_id for r in rows] == ["c1", "c2"]
    assert all(r.name == DISPATCH_TOOL_NAME for r in rows)
    payload = json.loads(rows[0].content)
    assert "任务A 的结论" in payload["results"][0]["summary"]
    assert payload["results"][0]["artifacts"][0]["action"] == "created"
    assert "未经核实" in payload["note"]
    assert out["subagent_consumed"] == 2
    assert out["pending_subtasks"] == []
    assert out["dispatch_rounds"] == 1


def test_merge_groups_multiple_subtasks_of_one_call():
    """一次声明调用里带多个子任务时，仍是一条 ToolMessage（协议一一对应）。"""
    out = _merge_node(
        _state([_result("A", "c1"), _result("B", "c1")], consumed=0)
    )
    rows = _tool_rows(out["messages"])
    assert len(rows) == 1
    payload = json.loads(rows[0].content)
    assert [r["task"] for r in payload["results"]] == ["A", "B"]


def test_merge_skips_already_consumed_results():
    """第二轮：state 累积 3 条，前 2 条上轮已合并，本轮只应出现任务C。"""
    results = [_result("任务A", "c1"), _result("任务B", "c2"), _result("任务C", "c3")]
    out = _merge_node(_state(results, consumed=2))
    rows = _tool_rows(out["messages"])
    assert len(rows) == 1
    assert rows[0].tool_call_id == "c3"
    assert "任务C" in rows[0].content
    assert "任务A" not in rows[0].content


def test_merge_no_op_when_nothing_new():
    """恢复/重入场景：消费游标已到末尾时不产出结果行，但仍推进派发轮数。"""
    out = _merge_node(_state([_result("任务A")], consumed=1))
    assert _tool_rows(out["messages"]) == []
    assert out["dispatch_rounds"] == 1
    assert out["pending_subtasks"] == []


def test_merge_tolerates_cursor_beyond_results():
    """游标越界（不变量被破坏）时降级为全量，不抛异常。"""
    out = _merge_node(_state([_result("任务A")], consumed=9))
    rows = _tool_rows(out["messages"])
    assert len(rows) == 1
    assert "任务A" in rows[0].content
    assert out["subagent_consumed"] == 1
