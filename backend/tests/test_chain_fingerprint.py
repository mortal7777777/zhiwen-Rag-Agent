"""消息链指纹单测：纯追加链的前缀比对可定位分歧点（缓存前缀诊断）。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.utils import _chain_fingerprints, _chain_preview


def _base_chain():
    return [
        SystemMessage(content="静态核心"),
        SystemMessage(content="技能目录+文档清单"),
        SystemMessage(content="项目记忆"),
        HumanMessage(content="问题一"),
        AIMessage(
            content="",
            tool_calls=[{"id": "c1", "name": "search", "args": {"q": "x"}}],
            additional_kwargs={"reasoning_content": "思考"},
        ),
        ToolMessage(content="结果一", tool_call_id="c1", name="search"),
        AIMessage(content="回答一"),
    ]


def test_fingerprints_stable_for_same_messages():
    assert _chain_fingerprints(_base_chain()) == _chain_fingerprints(_base_chain())


def test_pure_append_keeps_prefix_identical():
    """下一轮 = 上一轮 + 新增：前缀指纹必须逐条一致（纯追加不变量）。"""
    prev = _base_chain()
    prev_prints = _chain_fingerprints(prev)

    cur = prev + [SystemMessage(content="新 D 块"), HumanMessage(content="问题二")]
    cur_prints = _chain_fingerprints(cur)

    assert cur_prints[: len(prev_prints)] == prev_prints


def test_divergence_detected_at_first_changed_message():
    prev_prints = _chain_fingerprints(_base_chain())

    edited = _base_chain()
    edited[4] = AIMessage(
        content="",
        tool_calls=[{"id": "c1", "name": "search", "args": {"q": "x"}}],
        additional_kwargs={"reasoning_content": ""},  # reasoning 丢失
    )
    cur_prints = _chain_fingerprints(edited + [HumanMessage(content="问题二")])

    divergence = next(
        (i for i in range(len(prev_prints)) if prev_prints[i] != cur_prints[i]),
        None,
    )
    assert divergence == 4  # 第 5 条（工具轮助手消息）起断


def test_content_and_tool_call_changes_are_sensitive():
    base = _chain_fingerprints([HumanMessage(content="甲")])
    assert base != _chain_fingerprints([HumanMessage(content="乙")])

    call_a = AIMessage(content="", tool_calls=[{"id": "c1", "name": "t", "args": {"a": 1}}])
    call_b = AIMessage(content="", tool_calls=[{"id": "c1", "name": "t", "args": {"a": 2}}])
    assert _chain_fingerprints([call_a]) != _chain_fingerprints([call_b])


def test_chain_preview_is_single_line_and_truncated():
    msg = SystemMessage(content="第一行\n第二行 " + "长" * 200)
    preview = _chain_preview(msg)
    assert "\n" not in preview
    assert len(preview) <= 81
