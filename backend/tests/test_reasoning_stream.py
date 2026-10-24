"""思考链流式映射：ChatOpenAI 子类把 reasoning_content 保留进 additional_kwargs。"""

from __future__ import annotations

from langchain_core.messages import AIMessageChunk

from app.runtime_config import _chat_openai_cls


def _chunk(delta: dict) -> dict:
    return {
        "id": "chunk-1",
        "object": "chat.completion.chunk",
        "model": "deepseek-v4.1-flash",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }


def test_reasoning_content_is_preserved_for_streaming():
    model = _chat_openai_cls()(
        api_key="test-key",
        base_url="http://127.0.0.1:9/v1",
        model="deepseek-v4.1-flash",
    )
    result = model._convert_chunk_to_generation_chunk(
        _chunk({"reasoning_content": "先想一下", "content": ""}),
        AIMessageChunk,
        None,
    )
    assert result is not None
    assert result.message.additional_kwargs["reasoning_content"] == "先想一下"


def test_reasoning_field_alias_is_preserved():
    model = _chat_openai_cls()(
        api_key="test-key",
        base_url="http://127.0.0.1:9/v1",
        model="deepseek-v4.1-flash",
    )
    result = model._convert_chunk_to_generation_chunk(
        _chunk({"reasoning": "alias", "content": ""}),
        AIMessageChunk,
        None,
    )
    assert result is not None
    assert result.message.additional_kwargs["reasoning_content"] == "alias"


def test_normal_content_chunk_is_unchanged():
    model = _chat_openai_cls()(
        api_key="test-key",
        base_url="http://127.0.0.1:9/v1",
        model="deepseek-v4.1-flash",
    )
    result = model._convert_chunk_to_generation_chunk(
        _chunk({"content": "正文"}),
        AIMessageChunk,
        None,
    )
    assert result is not None
    assert result.message.content == "正文"
    assert "reasoning_content" not in result.message.additional_kwargs
