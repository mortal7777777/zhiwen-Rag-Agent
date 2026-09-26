"""Anthropic 格式的思考回传：工具轮必须把 thinking 块（含 signature）原样带回。

2026-09-26 实测：DeepSeek /anthropic 端点上思考以 content 块返回、additional_kwargs
为空，而回传逻辑只读 OpenAI 风格的 reasoning_content → 思考被丢掉 → 400
"The `content[].thinking` in the thinking mode must be passed back to the API"。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.utils import _rows_to_history
from app.llm_text import extract_thinking_blocks
from app.runtime_config import _AnthropicSystemNormalizer

_THINK = {
    "type": "thinking",
    "thinking": "用户在让我回显，我该调用 echo",
    "signature": "sig-abc",
}


def _tool_call(call_id="c1"):
    return [
        {
            "name": "echo",
            "args": {"text": "hi"},
            "id": call_id,
            "type": "tool_call",
        }
    ]


# ---------------- 抽取 ----------------


def test_extract_thinking_blocks_keeps_signature():
    content = [
        {"type": "thinking", **_THINK},
        {"type": "tool_use", "id": "t1", "name": "echo", "input": {}},
    ]
    blocks = extract_thinking_blocks(content)
    assert blocks == [_THINK]
    assert blocks[0]["signature"] == "sig-abc"


def test_extract_thinking_blocks_skips_empty_and_non_list():
    assert extract_thinking_blocks("纯字符串") == []
    assert extract_thinking_blocks(None) == []
    assert extract_thinking_blocks([{"type": "thinking", "thinking": "   "}]) == []
    assert extract_thinking_blocks([{"type": "text", "text": "正文"}]) == []


# ---------------- 请求时还原 ----------------


def _ai(blocks=None, content="", call_id="c1"):
    kwargs = {"reasoning_content": ""}
    if blocks is not None:
        kwargs["anthropic_thinking"] = blocks
    return AIMessage(
        content=content, tool_calls=_tool_call(call_id), additional_kwargs=kwargs
    )


def test_inject_thinking_into_tool_round():
    msgs = [HumanMessage(content="回显"), _ai([_THINK]), ToolMessage(content="{}", tool_call_id="c1")]
    out = _AnthropicSystemNormalizer._inject_thinking(msgs)
    ai = [m for m in out if isinstance(m, AIMessage)][0]
    assert [b["type"] for b in ai.content] == ["thinking"]
    assert ai.content[0]["signature"] == "sig-abc"


def test_inject_thinking_keeps_text_block():
    msgs = [_ai([_THINK], content="过渡说明")]
    out = _AnthropicSystemNormalizer._inject_thinking(msgs)
    ai = out[0]
    assert [b["type"] for b in ai.content] == ["thinking", "text"]
    assert ai.content[1]["text"] == "过渡说明"


def test_inject_thinking_idempotent_and_deterministic():
    msgs = [_ai([_THINK])]
    once = _AnthropicSystemNormalizer._inject_thinking(msgs)
    twice = _AnthropicSystemNormalizer._inject_thinking(once)
    assert twice[0].content == once[0].content, "重复规范化不得叠加思考块"
    # 同一输入两次调用结果一致（前缀缓存要求逐字节稳定）
    assert (
        _AnthropicSystemNormalizer._inject_thinking(msgs)[0].content
        == once[0].content
    )


def test_inject_thinking_leaves_other_messages_untouched():
    plain = _ai(None)
    msgs = [HumanMessage(content="你好"), plain]
    out = _AnthropicSystemNormalizer._inject_thinking(msgs)
    assert out is msgs, "没有可注入的思考块时应原样返回（不做无谓拷贝）"
    assert out[1].content == ""


# ---------------- 落库重建 ----------------


def test_rows_to_history_restores_thinking_marker():
    import json

    rows = [
        {"role": "user", "content": "回显"},
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "__tool_calls__": _tool_call(),
                    "__reasoning__": "",
                    "__thinking__": [_THINK],
                },
                ensure_ascii=False,
            ),
            "tool_trace": None,
        },
        {
            "role": "tool",
            "content": "{}",
            "tool_trace": [{"tool_call_id": "c1", "name": "echo"}],
        },
    ]
    history = _rows_to_history(rows, [])
    ai = [m for m in history if isinstance(m, AIMessage)][0]
    assert ai.additional_kwargs["anthropic_thinking"] == [_THINK]


def test_rows_to_history_without_thinking_marker():
    import json

    rows = [
        {
            "role": "assistant",
            "content": json.dumps({"__tool_calls__": _tool_call(), "__reasoning__": "r"}),
            "tool_trace": None,
        },
        {
            "role": "tool",
            "content": "{}",
            "tool_trace": [{"tool_call_id": "c1", "name": "echo"}],
        },
    ]
    history = _rows_to_history(rows, [])
    ai = [m for m in history if isinstance(m, AIMessage)][0]
    assert "anthropic_thinking" not in ai.additional_kwargs
    assert ai.additional_kwargs["reasoning_content"] == "r"
