"""节点直调用的 runtime 替身（测试用）。

真实运行时，LangGraph 按参数名把 Runtime 注入节点第二参数，请求级依赖在
`runtime.context`（Deps）。节点级测试不走图，所以这里用一个只带 `context`
的替身；依赖优先从测试 state 里的旧字段取（沿用各文件的 `_state()` 构造习惯）。
"""

from __future__ import annotations

from types import SimpleNamespace


def node_runtime(**deps) -> SimpleNamespace:
    return SimpleNamespace(context=SimpleNamespace(**deps))


def runtime_from_state(state: dict) -> SimpleNamespace:
    """按 state 里的旧字段构造 Deps 替身（缺的字段为 None，节点用到才会报错）。"""
    return node_runtime(
        service=state.get("service"),
        bus=state.get("bus"),
        db=state.get("db"),
        stop_event=state.get("stop_event"),
        tools=state.get("tools") or [],
        title_holder=state.get("title_holder") or [],
        title_thread=state.get("title_thread"),
    )
