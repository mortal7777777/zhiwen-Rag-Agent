"""Anthropic 兼容格式接入的纯逻辑单测：内容归一化 / 格式判定 / 模型工厂。"""

from __future__ import annotations

from app.llm_text import message_text
from app.runtime_config import build_chat_model, provider_api_format, thinking_param


def test_message_text_passthrough_str():
    assert message_text("你好") == "你好"
    assert message_text(None) == ""
    assert message_text("") == ""


def test_message_text_anthropic_blocks():
    content = [
        {"type": "thinking", "thinking": "思考中…"},  # 非正文块不计入
        {"type": "text", "text": "正文"},
        {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
        "片段",
    ]
    assert message_text(content) == "正文片段"


def test_provider_api_format():
    assert provider_api_format({"base_url": "https://api.deepseek.com"}) == "openai"
    assert (
        provider_api_format({"base_url": "https://api.deepseek.com/anthropic"})
        == "anthropic"
    )
    # 显式字段优先于 URL 推断
    assert (
        provider_api_format({"base_url": "https://example.com", "api_format": "anthropic"})
        == "anthropic"
    )
    assert provider_api_format({"base_url": "x", "api_format": "openai"}) == "openai"
    assert provider_api_format(None) == "openai"


def test_thinking_param():
    assert thinking_param({"thinking_enabled": False}) == {"type": "disabled"}
    assert thinking_param({})["type"] == "enabled"
    assert thinking_param(None)["type"] == "enabled"


def test_models_request_path_and_headers():
    from app.api.settings import _models_candidates

    # Anthropic 格式：主端点用 x-api-key；随后剥离 /anthropic 回退根端点
    cands = _models_candidates(
        {"base_url": "https://api.deepseek.com/anthropic", "api_key": "k"}
    )
    assert cands[0][0] == "https://api.deepseek.com/anthropic/v1/models"
    assert cands[0][1]["x-api-key"] == "k"
    assert "Authorization" not in cands[0][1]
    assert cands[1][0] == "https://api.deepseek.com/models"
    assert cands[1][1]["Authorization"] == "Bearer k"
    assert cands[2][0] == "https://api.deepseek.com/v1/models"

    # OpenAI 格式：只有主端点（无兼容后缀可剥离）
    cands = _models_candidates(
        {"base_url": "https://api.deepseek.com", "api_key": "k"}
    )
    assert cands == [
        ("https://api.deepseek.com/models", {"Authorization": "Bearer k"})
    ]

    # 长后缀优先：/api/anthropic 整段剥离，而不是只剩 /api
    cands = _models_candidates(
        {
            "base_url": "https://proxy.example.com/api/anthropic",
            "api_key": "k",
            "api_format": "anthropic",
        }
    )
    assert cands[1][0] == "https://proxy.example.com/models"


def test_build_chat_model_picks_client_by_format():
    anth = build_chat_model(
        {
            "api_key": "k",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "deepseek-v4-flash",
        },
        thinking={"type": "disabled"},
    )
    # anthropic 格式外包一层"链中非连续 system 归一化"适配器（2026-09-18 修复），
    # 内层是带代理回退客户端的 ChatAnthropic 子类（同日第二个修复）
    assert type(anth).__name__ == "_AnthropicSystemNormalizer"
    assert type(anth._inner).__name__ == "_FallbackChatAnthropic"
    op = build_chat_model(
        {"api_key": "k", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash"}
    )
    assert type(op).__name__ == "ChatOpenAI"


def test_build_chat_model_uses_proxy_fallback_clients():
    """chat 模型的同步/异步 httpx 客户端都必须是带系统代理回退的（2026-09-18）。"""
    from app.network import AsyncProxyFallbackTransport, ProxyFallbackTransport

    op = build_chat_model(
        {"api_key": "k", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash"}
    )
    assert isinstance(op.http_client._transport, ProxyFallbackTransport)
    assert isinstance(op.http_async_client._transport, AsyncProxyFallbackTransport)

    anth = build_chat_model(
        {
            "api_key": "k",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "deepseek-v4-flash",
        },
        thinking={"type": "disabled"},
    )
    # 触发内部客户端构造（不发起网络请求），确认走的是回退工厂；
    # anthropic SDK 用 httpx2，客户端类型断言用 httpx2 变体
    from app.network import AsyncProxyFallbackTransport2, ProxyFallbackTransport2

    sdk_client = anth._inner._client
    assert isinstance(sdk_client._client._transport, ProxyFallbackTransport2)
    assert isinstance(
        anth._inner._async_client._client._transport, AsyncProxyFallbackTransport2
    )
