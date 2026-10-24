"""merge 节点单测：子任务结果按 tool_call 回填成 ToolMessage。

三条要点：
1. 只消费本轮新增结果——游标存在 runtime（每轮新建），通道本身由 prepare
   的 reset 归约保证每轮清空（见 state.merge_subagent_results）。
2. 结果以 **ToolMessage**（工具返回值）进入主 agent 上下文，而不是 system
   消息断言"（已完成）"——2026-09-24 事故正是 system 断言放大了子代理的
   错误结论（它把磁盘上的历史遗留产物当成成果上报）。
3. 空派发分支手工构造的 Send 载荷必须覆盖 merge 硬读键（契约测试）。
"""

from __future__ import annotations

import ast
import inspect
import json
from queue import Queue

from langchain_core.messages import ToolMessage

from app.agent.nodes.subagent import (
    DISPATCH_TOOL_NAME,
    _dispatch_tasks,
    _merge_node,
)
from app.agent.state import RESULTS_RESET, EventBus, merge_subagent_results
from _node_rt import runtime_from_state  # noqa: E402


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


def _state(results: list[dict], merged: int) -> dict:
    queue: Queue = Queue()
    return {
        "bus": EventBus(queue),
        "_events": queue,
        "db": None,
        "runtime": {"subagent_merged": merged},
        "messages": [],
        "subagent_results": results,
        "counter": [0],
        "tool_trace": [],
        "sources": [],
        "todos": [],
        "dispatch_rounds": 0,
    }


def _tool_rows(messages: list) -> list[ToolMessage]:
    return [m for m in messages if isinstance(m, ToolMessage)]


def _drain_events(state: dict) -> list[dict]:
    out: list[dict] = []
    while not state["_events"].empty():
        out.append(state["_events"].get())
    return out


def test_merge_emits_tool_messages_with_evidence():
    st = _state([_result("任务A", "c1"), _result("任务B", "c2")], merged=0)
    out = _merge_node(st, runtime_from_state(st))
    rows = _tool_rows(out["messages"])
    assert [r.tool_call_id for r in rows] == ["c1", "c2"]
    assert all(r.name == DISPATCH_TOOL_NAME for r in rows)
    payload = json.loads(rows[0].content)
    assert "任务A 的结论" in payload["results"][0]["summary"]
    assert payload["results"][0]["artifacts"][0]["action"] == "created"
    assert "未经核实" in payload["note"]
    assert st["runtime"]["subagent_merged"] == 2
    assert out["pending_subtasks"] == []
    assert out["dispatch_rounds"] == 1


def test_merge_groups_multiple_subtasks_of_one_call():
    """一次声明调用里带多个子任务时，仍是一条 ToolMessage（协议一一对应）。"""
    st = _state([_result("A", "c1"), _result("B", "c1")], merged=0)
    out = _merge_node(st, runtime_from_state(st))
    rows = _tool_rows(out["messages"])
    assert len(rows) == 1
    payload = json.loads(rows[0].content)
    assert [r["task"] for r in payload["results"]] == ["A", "B"]


def test_merge_remaps_citation_indices_in_summary():
    """来源重编号后，摘要正文里的 [n] 必须指向同一全局编号。

    子代理各自从 1 起编号；两个并行分支都写"见[1]"时，第二个分支的正文
    要改写成全局编号，否则主 agent 的引用会指向别的分支的来源。
    """
    first = _result("任务A", "c1")
    first["sources"] = [{"index": 1, "type": "kb", "content": "A"}]
    first["summary"] = "根据[1]，结论是 A；另见[9]"
    second = _result("任务B", "c2")
    second["sources"] = [{"index": 1, "type": "kb", "content": "B"}]
    second["summary"] = "参见[1]"

    st = _state([first, second], merged=0)
    out = _merge_node(st, runtime_from_state(st))
    rows = _tool_rows(out["messages"])
    payloads = [json.loads(r.content) for r in rows]

    assert payloads[0]["results"][0]["summary"] == "根据[1]，结论是 A；另见[9]"
    assert payloads[1]["results"][0]["summary"] == "参见[2]"
    assert payloads[0]["results"][0]["sources"] == [1]
    assert payloads[1]["results"][0]["sources"] == [2]


def test_sse_tool_result_carries_source_objects_not_indices():
    """SSE 事件必须发完整来源对象；模型侧 payload 才用编号数组。

    前端 onToolResult 会把这些对象直接放进来源列表，发整数会渲染成空卡片。
    """
    first = _result("任务A", "c1")
    first["sources"] = [
        {"index": 1, "type": "kb", "content": "A", "source": "a.md", "page": 3}
    ]
    second = _result("任务B", "c2")
    second["sources"] = [{"index": 1, "type": "kb", "content": "B"}]

    st = _state([first, second], merged=0)
    _merge_node(st, runtime_from_state(st))

    events = [e for e in _drain_events(st) if e["event"] == "tool_result"]
    assert [e["data"]["name"] for e in events] == ["subagent", "subagent"]
    assert events[0]["data"]["sources"] == [
        {"index": 1, "type": "kb", "content": "A", "source": "a.md", "page": 3}
    ]
    assert events[1]["data"]["sources"] == [
        {"index": 2, "type": "kb", "content": "B"}
    ]


def test_merge_skips_already_consumed_results():
    """第二轮：本轮累积 3 条，前 2 条上轮已合并，本轮只应出现任务C。"""
    results = [_result("任务A", "c1"), _result("任务B", "c2"), _result("任务C", "c3")]
    st = _state(results, merged=2)
    out = _merge_node(st, runtime_from_state(st))
    rows = _tool_rows(out["messages"])
    assert len(rows) == 1
    assert rows[0].tool_call_id == "c3"
    assert "任务C" in rows[0].content
    assert "任务A" not in rows[0].content


def test_merge_no_op_when_nothing_new():
    """恢复/重入场景：消费游标已到末尾时不产出结果行，但仍推进派发轮数。"""
    st = _state([_result("任务A")], merged=1)
    out = _merge_node(st, runtime_from_state(st))
    assert _tool_rows(out["messages"]) == []
    assert out["dispatch_rounds"] == 1
    assert out["pending_subtasks"] == []


def test_merge_tolerates_cursor_beyond_results():
    """游标越界（不变量被破坏）时降级为全量，不抛异常。"""
    st = _state([_result("任务A")], merged=9)
    out = _merge_node(st, runtime_from_state(st))
    rows = _tool_rows(out["messages"])
    assert len(rows) == 1
    assert "任务A" in rows[0].content
    assert st["runtime"]["subagent_merged"] == 1


# ---------------- 归约器：整轮清空 + 追加 ----------------


def test_results_reducer_reset_then_append():
    """reset 标记清空遗留值；随后分支追加照常生效（prepare 每轮先发 reset）。"""
    carried = [{"task": "上一轮的结论"}]
    assert merge_subagent_results(carried, [RESULTS_RESET]) == []
    assert merge_subagent_results(None, [RESULTS_RESET]) == []
    assert merge_subagent_results(carried, [{"task": "本轮"}]) == carried + [
        {"task": "本轮"}
    ]
    # 清空后再追加：通道里只剩本轮结果（跨轮残留从源头消除）
    cleared = merge_subagent_results(carried, [RESULTS_RESET])
    assert merge_subagent_results(cleared, [{"task": "本轮"}]) == [{"task": "本轮"}]


# ---------------- 契约：Send 载荷覆盖 merge 硬读键 ----------------


def test_dispatch_send_payload_carries_conversation_id():
    """子代理分支 state 需要会话号：skill_view 的用量归因读 conversation_id。"""
    sends = _dispatch_tasks(
        {
            "pending_subtasks": [{"task": "t", "mode": "research", "call_id": "c1"}],
            "question": "q",
            "runtime": {},
            "project_dir": None,
            "conversation_id": 42,
        }
    )
    assert len(sends) == 1 and sends[0].node == "subagent"
    assert sends[0].arg["conversation_id"] == 42


def test_merge_send_payload_covers_required_keys():
    """空派发分支手工构造的 Send 载荷必须带齐 merge 用 `state["x"]` 硬读的键。

    用 AST 从 merge 源码提取硬读键（非 .get），以后 merge 新增读取点即自动
    受检——这条异常路径最容易在重构中被漏掉，KeyError 会直接吃掉整轮回答。
    """
    tree = ast.parse(inspect.getsource(_merge_node))
    required = {
        node.slice.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "state"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    }
    assert required, "未能从 merge 源码解析出硬读键，测试本身需要更新"

    # 活对象（service/bus/db）已走 Runtime context：载荷只需覆盖 state 硬读键
    sends = _dispatch_tasks({"pending_subtasks": [], "runtime": {}})
    assert len(sends) == 1 and sends[0].node == "merge"
    missing = required - set(sends[0].arg.keys())
    assert not missing, f"空派发 Send 载荷缺少 merge 硬读键：{sorted(missing)}"
