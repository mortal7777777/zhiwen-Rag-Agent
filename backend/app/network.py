"""出站 HTTP 代理容错：系统代理不可用时自动回退直连。

httpx 默认 trust_env 会读取 Windows 系统代理（注册表 ProxyServer，如
127.0.0.1:7890）：代理软件（Clash 等）未启动时，所有出站请求直接报
WinError 10061 连接被拒——代理不应成为强制前提。

本模块提供 ProxyFallbackTransport：请求先按系统代理发送，代理连接被拒
（ConnectError）时自动回退直连（进程内只回退一次，之后直接走直连），
并统一提供客户端工厂。回环地址（localhost）不在代理检测范围内，
OpenSearch/SearXNG 等本地服务请继续使用普通 httpx.Client。
"""

from __future__ import annotations

import logging
import urllib.request

import httpx

logger = logging.getLogger(__name__)


def system_proxy() -> str | None:
    """读取系统/环境代理（https 优先，回退 http）。"""
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return None
    return proxies.get("https") or proxies.get("http")


class ProxyFallbackTransport(httpx.BaseTransport):
    """优先走系统代理，代理连接被拒时回退直连。"""

    def __init__(self) -> None:
        proxy = system_proxy()
        self._direct = httpx.HTTPTransport()
        self._proxied = httpx.HTTPTransport(proxy=proxy) if proxy else None
        self._fallen_back = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self._proxied is not None and not self._fallen_back:
            try:
                return self._proxied.handle_request(request)
            except httpx.ConnectError as exc:
                self._fallen_back = True
                logger.warning("系统代理不可用（%s），出站请求回退直连", exc)
        return self._direct.handle_request(request)

    def close(self) -> None:
        self._direct.close()
        if self._proxied is not None:
            self._proxied.close()


def make_httpx_client(**kwargs) -> httpx.Client:
    """创建带代理回退的 httpx 客户端（用于远端 API 出站调用）。

    用法与 httpx.Client 一致（timeout 等参数透传）；
    回环/本地服务不需要代理，请直接用 httpx.Client。
    """
    headers = dict(kwargs.pop("headers", None) or {})
    # 同 rag/store.py：httpx 0.28 声明 zstd 但无解码器，远端回 zstd 时挂起
    headers.setdefault("Accept-Encoding", "gzip, deflate")
    return httpx.Client(
        transport=ProxyFallbackTransport(),
        trust_env=False,
        headers=headers,
        **kwargs,
    )


class AsyncProxyFallbackTransport(httpx.AsyncBaseTransport):
    """ProxyFallbackTransport 的异步版（供 AsyncClient 与异步 SDK 用）。

    2026-09-18 实测教训：LLM SDK（anthropic/openai）自建的 httpx 客户端在
    构造时捕获系统代理（如 127.0.0.1:7890）——Clash 关闭后客户端仍打向
    死代理，所有模型调用报 "Connection error." 且重试无效（客户端不重建）。
    故 chat 模型的同步/异步客户端统一改走本模块的工厂。
    """

    def __init__(self) -> None:
        proxy = system_proxy()
        self._direct = httpx.AsyncHTTPTransport()
        self._proxied = httpx.AsyncHTTPTransport(proxy=proxy) if proxy else None
        self._fallen_back = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._proxied is not None and not self._fallen_back:
            try:
                return await self._proxied.handle_async_request(request)
            except httpx.ConnectError as exc:
                self._fallen_back = True
                logger.warning("系统代理不可用（%s），异步出站请求回退直连", exc)
        return await self._direct.handle_async_request(request)

    async def aclose(self) -> None:
        await self._direct.aclose()
        if self._proxied is not None:
            await self._proxied.aclose()


def make_async_httpx_client(**kwargs) -> httpx.AsyncClient:
    """创建带代理回退的异步 httpx 客户端（异步 SDK 出站调用用）。"""
    headers = dict(kwargs.pop("headers", None) or {})
    headers.setdefault("Accept-Encoding", "gzip, deflate")
    return httpx.AsyncClient(
        transport=AsyncProxyFallbackTransport(),
        trust_env=False,
        headers=headers,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# httpx2 变体：本环境的 anthropic SDK 校验 http_client 必须是 httpx2.Client
# （httpx 的 Client 会被拒收："this SDK uses httpx2"），故单独提供一组。
# ---------------------------------------------------------------------------

try:  # 未安装 httpx2 的环境仅 anthropic 路径受影响
    import httpx2

    class ProxyFallbackTransport2(httpx2.BaseTransport):
        """httpx2 版代理回退传输（anthropic SDK 客户端用）。"""

        def __init__(self) -> None:
            proxy = system_proxy()
            self._direct = httpx2.HTTPTransport(trust_env=False)
            self._proxied = (
                httpx2.HTTPTransport(proxy=proxy, trust_env=False) if proxy else None
            )
            self._fallen_back = False

        def handle_request(self, request: httpx2.Request) -> httpx2.Response:
            if self._proxied is not None and not self._fallen_back:
                try:
                    return self._proxied.handle_request(request)
                except httpx2.ConnectError as exc:
                    self._fallen_back = True
                    logger.warning("系统代理不可用（%s），出站请求回退直连", exc)
            return self._direct.handle_request(request)

        def close(self) -> None:
            self._direct.close()
            if self._proxied is not None:
                self._proxied.close()

    class AsyncProxyFallbackTransport2(httpx2.AsyncBaseTransport):
        """httpx2 版异步代理回退传输。"""

        def __init__(self) -> None:
            proxy = system_proxy()
            self._direct = httpx2.AsyncHTTPTransport(trust_env=False)
            self._proxied = (
                httpx2.AsyncHTTPTransport(proxy=proxy, trust_env=False) if proxy else None
            )
            self._fallen_back = False

        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            if self._proxied is not None and not self._fallen_back:
                try:
                    return await self._proxied.handle_async_request(request)
                except httpx2.ConnectError as exc:
                    self._fallen_back = True
                    logger.warning("系统代理不可用（%s），异步出站请求回退直连", exc)
            return await self._direct.handle_async_request(request)

        async def aclose(self) -> None:
            await self._direct.aclose()
            if self._proxied is not None:
                await self._proxied.aclose()

    def make_httpx2_client(**kwargs) -> "httpx2.Client":
        """创建带代理回退的 httpx2 客户端（anthropic SDK 出站调用用）。"""
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("Accept-Encoding", "gzip, deflate")
        return httpx2.Client(
            transport=ProxyFallbackTransport2(),
            trust_env=False,
            headers=headers,
            **kwargs,
        )

    def make_async_httpx2_client(**kwargs) -> "httpx2.AsyncClient":
        """创建带代理回退的 httpx2 异步客户端。"""
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("Accept-Encoding", "gzip, deflate")
        return httpx2.AsyncClient(
            transport=AsyncProxyFallbackTransport2(),
            trust_env=False,
            headers=headers,
            **kwargs,
        )

except ImportError:  # pragma: no cover
    httpx2 = None
