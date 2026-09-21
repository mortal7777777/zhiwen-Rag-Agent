"""Anthropic 格式消息归一化测试：链中非连续 system → user（保前缀稳定）。

背景：langchain_anthropic 只允许一个 system 消息，项目纯追加链中的
merge 摘要/进度提示等 system 行会触发 ValueError（2026-09-18 实测）。
"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.runtime_config import _AnthropicSystemNormalizer


def _norm(msgs):
    return _AnthropicSystemNormalizer._normalize(msgs)


def test_leading_system_segment_kept():
    msgs = [
        SystemMessage(content="核心"),
        SystemMessage(content="技能目录"),
        HumanMessage(content="问题"),
    ]
    out = _norm(msgs)
    assert [m.type for m in out] == ["system", "system", "human"]


def test_mid_chain_system_demoted_to_human():
    msgs = [
        SystemMessage(content="核心"),
        HumanMessage(content="Q"),
        AIMessage(content="a"),
        SystemMessage(content="合并摘要"),
    ]
    out = _norm(msgs)
    assert [m.type for m in out] == ["system", "human", "ai", "human"]
    assert out[-1].content == "合并摘要"
    assert out[0].content == "核心"


def test_multiple_trailing_systems_all_demoted():
    msgs = [
        SystemMessage(content="核心"),
        HumanMessage(content="Q"),
        SystemMessage(content="todos"),
        SystemMessage(content="时间"),
    ]
    out = _norm(msgs)
    assert [m.type for m in out] == ["system", "human", "human", "human"]


def test_deterministic_same_input_same_output():
    msgs = [
        SystemMessage(content="s"),
        HumanMessage(content="q"),
        SystemMessage(content="x"),
    ]
    assert _norm(msgs) == _norm(msgs)


def test_non_list_passthrough():
    assert _norm("hello") == "hello"


def test_no_conversion_returns_original_list():
    msgs = [SystemMessage(content="s"), HumanMessage(content="q")]
    assert _norm(msgs) is msgs


class _FakeInner:
    """记录收到的消息类型序列的假模型。"""

    def __init__(self):
        self.seen = None

    def invoke(self, input, config=None, **kwargs):
        self.seen = [m.type for m in input]
        return "ok"

    def stream(self, input, config=None, **kwargs):
        self.seen = [m.type for m in input]
        yield "chunk"

    def bind_tools(self, tools, **kwargs):
        self.bound = tools
        return self


def test_wrapper_invoke_normalizes():
    inner = _FakeInner()
    wrapper = _AnthropicSystemNormalizer(inner)
    result = wrapper.invoke(
        [SystemMessage(content="s"), HumanMessage(content="q"), SystemMessage(content="x")]
    )
    assert result == "ok"
    assert inner.seen == ["system", "human", "human"]


def test_wrapper_bind_tools_still_normalizes():
    inner = _FakeInner()
    wrapper = _AnthropicSystemNormalizer(inner)
    bound = wrapper.bind_tools(["tool_a"])
    bound.invoke([HumanMessage(content="q"), SystemMessage(content="x")])
    assert inner.seen == ["human", "human"]
    assert inner.bound == ["tool_a"]
