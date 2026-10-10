"""Anthropic 兼容格式接入的纯逻辑单测：内容归一化 / 格式判定 / 模型工厂。"""

from __future__ import annotations

from app.llm_text import message_text
from app.runtime_config import (
    build_chat_model,
    normalize_base_url,
    provider_api_format,
    provider_extra_headers,
    thinking_param,
)


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

    # 用户把完整端点当 base_url 粘贴（OpenCode 文档式）：剥掉 /messages 后
    # 首个候选即 Go 网关真实可用的模型列表端点
    cands = _models_candidates(
        {"base_url": "https://opencode.ai/zen/go/v1/messages", "api_key": "k"}
    )
    assert cands[0][0] == "https://opencode.ai/zen/go/v1/models"


def test_normalize_base_url():
    # openai：剥掉完整端点后缀，保留 SDK 需要的 /v1
    assert (
        normalize_base_url("https://opencode.ai/zen/go/v1/messages", "openai")
        == "https://opencode.ai/zen/go/v1"
    )
    assert (
        normalize_base_url("https://api.example.com/v1/chat/completions", "openai")
        == "https://api.example.com/v1"
    )
    # anthropic：整段 /v1/messages 剥掉回根（SDK 自己会拼 /v1/messages）
    assert (
        normalize_base_url("https://opencode.ai/zen/go/v1/messages", "anthropic")
        == "https://opencode.ai/zen/go"
    )
    # 正常 base 不受影响
    assert normalize_base_url("https://api.deepseek.com", "openai") == "https://api.deepseek.com"
    assert (
        normalize_base_url("https://api.deepseek.com/anthropic", "anthropic")
        == "https://api.deepseek.com/anthropic"
    )
    assert normalize_base_url(None, "openai") == "https://api.deepseek.com"


def test_provider_extra_headers_opencode():
    cfg = {"api_key": "oc_sk_test", "base_url": "https://opencode.ai/zen/go/v1"}
    headers = provider_extra_headers(cfg, "https://opencode.ai/zen/go/v1")
    assert headers["x-opencode-session"].startswith("zhiwen-")
    # 稳定派生：同 key/base 两次调用一致（跨进程重启不变）
    assert headers == provider_extra_headers(cfg, "https://opencode.ai/zen/go/v1")
    # 其他供应商不加任何头
    assert provider_extra_headers({"api_key": "k"}, "https://api.deepseek.com") == {}
    assert provider_extra_headers(None, "https://opencode.ai/zen/go/v1") == {}


def test_build_chat_model_opencode_identifies_client():
    """OpenCode 网关要求自报身份：Go 端点缺 x-opencode-session 直接 400。"""
    op = build_chat_model(
        {
            "api_key": "oc_sk_test",
            "base_url": "https://opencode.ai/zen/go/v1/messages",
            "model": "deepseek-v4.1-flash",
        }
    )
    # base_url 先归一化再使用；默认头带会话 ID 与自定义 UA
    assert str(op.openai_api_base) == "https://opencode.ai/zen/go/v1"
    assert op.default_headers["x-opencode-session"].startswith("zhiwen-")
    assert op.default_headers["User-Agent"] == "zhiwen-rag/1.0"
    # 非 OpenCode 供应商不受影响
    plain = build_chat_model(
        {"api_key": "k", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash"}
    )
    assert not plain.default_headers


def test_build_chat_model_merges_custom_headers_and_clamps_anthropic_temp():
    # 供应商自定义头覆盖自动注入（x-opencode-session 可被显式覆盖）
    op = build_chat_model(
        {
            "api_key": "k",
            "base_url": "https://opencode.ai/zen/go/v1",
            "model": "m",
            "headers": {"X-Trace": "t", "x-opencode-session": "custom"},
        }
    )
    assert op.default_headers["x-opencode-session"] == "custom"
    assert op.default_headers["X-Trace"] == "t"

    # Anthropic 协议温度上界 1：超界在构建点夹取（否则端点直接 400）
    anth = build_chat_model(
        {"api_key": "k", "base_url": "https://api.deepseek.com/anthropic", "model": "m"},
        temperature=2.0,
        thinking={"type": "disabled"},
    )
    assert anth._inner.temperature == 1.0


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
