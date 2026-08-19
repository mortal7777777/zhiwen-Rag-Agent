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
    return httpx.Client(transport=ProxyFallbackTransport(), trust_env=False, **kwargs)
