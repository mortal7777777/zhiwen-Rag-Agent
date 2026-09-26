"""tools 节点单测：计划进度行只在内容变化时追加。

tools 节点每轮 ReAct 循环都会走到进度回填那段。若无条件追加，内容未变时
也会重复塞一条同值 system 行（实测单 run 堆到 13 行），每行都是前缀缓存
miss 且此后常驻历史。
"""

from __future__ import annotations

from queue import Queue

from app.agent.nodes.tools import _tools_node
from app.agent.state import EventBus
from app.config import get_settings


class _Svc:
    settings = get_settings()


def _state() -> dict:
    return {
        "service": _Svc(),
        "bus": EventBus(Queue()),
        "runtime": {},
        "messages": [],
        "db": None,
        "tools": [],
        # 指向不存在的工具 -> 直接走失败分支，不推进计划进度
        "pending_tool_calls": [{"id": "c1", "name": "no_such_tool", "args": {}}],
        "plan_map": [{"step": "步骤一"}, {"step": "步骤二"}],
        "todos": [],
        "plan_done_count": 1,
        "tool_trace": [],
        "sources": [],
        "tool_calls_used": 0,
        "failure_count": 0,
        "forced_final": False,
        "last_call_warned": False,
    }


def _progress_lines(messages: list) -> list[str]:
    return [
        m.content for m in messages
        if getattr(m, "content", "").startswith("计划进度：")
    ]


def test_progress_line_appended_once_when_unchanged():
    state = _state()
    first = _tools_node(state)
    assert len(_progress_lines(first["messages"])) == 1

    # 第二轮循环：状态内容未变（同一份 plan_map / plan_done_count）
    state.update(first)
    state["messages"] = []
    state["pending_tool_calls"] = [{"id": "c2", "name": "no_such_tool", "args": {}}]
    second = _tools_node(state)
    assert _progress_lines(second["messages"]) == []
    assert state["plan_progress_sig"], "去重标记必须回写进 state"


def test_progress_line_appended_again_after_change():
    state = _state()
    first = _tools_node(state)
    state.update(first)
    state["messages"] = []
    state["pending_tool_calls"] = [{"id": "c2", "name": "no_such_tool", "args": {}}]
    # 进度发生变化：应重新追加
    state["plan_done_count"] = 2
    second = _tools_node(state)
    assert len(_progress_lines(second["messages"])) == 1
