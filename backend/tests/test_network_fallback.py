"""代理回退传输单测：系统代理连接被拒 → 自动回退直连（同步 + 异步）。

背景（2026-09-18）：LLM SDK 自建 httpx 客户端会捕获系统代理（Clash 7890），
代理关闭后模型调用报 "Connection error." 且重试无效——本模块的回退传输
被接到 chat 模型客户端上后，这类故障应自动降级直连。
"""

import asyncio

import httpx
import pytest

from app import network


class _SyncOk(httpx.BaseTransport):
    def __init__(self):
        self.calls = 0

    def handle_request(self, request):
        self.calls += 1
        return httpx.Response(200, request=request)


class _SyncReject(httpx.BaseTransport):
    def handle_request(self, request):
        raise httpx.ConnectError("proxy refused", request=request)


class _AsyncOk(httpx.AsyncBaseTransport):
    def __init__(self):
        self.calls = 0

    async def handle_async_request(self, request):
        self.calls += 1
        return httpx.Response(200, request=request)


class _AsyncReject(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        raise httpx.ConnectError("proxy refused", request=request)


def test_sync_transport_falls_back_on_proxy_refusal(monkeypatch):
    monkeypatch.setattr(network, "system_proxy", lambda: "http://127.0.0.1:9")
    transport = network.ProxyFallbackTransport()
    ok = _SyncOk()
    transport._proxied = _SyncReject()
    transport._direct = ok
    request = httpx.Request("GET", "https://example.com")

    assert transport.handle_request(request).status_code == 200
    assert transport._fallen_back is True
    # 回退只发生一次：第二次请求直接走直连
    assert transport.handle_request(request).status_code == 200
    assert ok.calls == 2


def test_async_transport_falls_back_on_proxy_refusal(monkeypatch):
    monkeypatch.setattr(network, "system_proxy", lambda: "http://127.0.0.1:9")
    transport = network.AsyncProxyFallbackTransport()
    ok = _AsyncOk()
    transport._proxied = _AsyncReject()
    transport._direct = ok
    request = httpx.Request("GET", "https://example.com")

    assert asyncio.run(transport.handle_async_request(request)).status_code == 200
    assert transport._fallen_back is True
    assert asyncio.run(transport.handle_async_request(request)).status_code == 200
    assert ok.calls == 2


def test_no_proxy_goes_direct(monkeypatch):
    monkeypatch.setattr(network, "system_proxy", lambda: None)
    transport = network.ProxyFallbackTransport()
    ok = _SyncOk()
    transport._direct = ok
    request = httpx.Request("GET", "https://example.com")
    assert transport.handle_request(request).status_code == 200
    assert ok.calls == 1


def test_make_async_client_uses_fallback_transport(monkeypatch):
    monkeypatch.setattr(network, "system_proxy", lambda: None)
    client = network.make_async_httpx_client()
    try:
        assert isinstance(client._transport, network.AsyncProxyFallbackTransport)
    finally:
        asyncio.run(client.aclose())


# ---------------- httpx2 变体（anthropic SDK 用） ----------------

def test_httpx2_transport_falls_back(monkeypatch):
    httpx2 = pytest.importorskip("httpx2")
    monkeypatch.setattr(network, "system_proxy", lambda: "http://127.0.0.1:9")
    transport = network.ProxyFallbackTransport2()

    class _Ok(httpx2.BaseTransport):
        def __init__(self):
            self.calls = 0

        def handle_request(self, request):
            self.calls += 1
            return httpx2.Response(200, request=request)

    class _Reject(httpx2.BaseTransport):
        def handle_request(self, request):
            raise httpx2.ConnectError("proxy refused", request=request)

    ok = _Ok()
    transport._proxied = _Reject()
    transport._direct = ok
    request = httpx2.Request("GET", "https://example.com")
    assert transport.handle_request(request).status_code == 200
    assert transport._fallen_back is True
    assert transport.handle_request(request).status_code == 200
    assert ok.calls == 2


def test_httpx2_async_transport_falls_back(monkeypatch):
    httpx2 = pytest.importorskip("httpx2")
    monkeypatch.setattr(network, "system_proxy", lambda: "http://127.0.0.1:9")
    transport = network.AsyncProxyFallbackTransport2()

    class _Ok(httpx2.AsyncBaseTransport):
        def __init__(self):
            self.calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            return httpx2.Response(200, request=request)

    class _Reject(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx2.ConnectError("proxy refused", request=request)

    ok = _Ok()
    transport._proxied = _Reject()
    transport._direct = ok
    request = httpx2.Request("GET", "https://example.com")
    assert asyncio.run(transport.handle_async_request(request)).status_code == 200
    assert asyncio.run(transport.handle_async_request(request)).status_code == 200
    assert ok.calls == 2


def test_make_httpx2_client_uses_fallback(monkeypatch):
    pytest.importorskip("httpx2")
    monkeypatch.setattr(network, "system_proxy", lambda: None)
    client = network.make_httpx2_client()
    try:
        assert isinstance(client._transport, network.ProxyFallbackTransport2)
    finally:
        client.close()
