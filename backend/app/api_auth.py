"""可选的全局 API 认证：设置 ZHIWEN_API_TOKEN 后，/api/* 请求需携带令牌。

为什么是纯 ASGI 中间件而不是 BaseHTTPMiddleware：Agent SSE 是长连接流式
响应，BaseHTTPMiddleware 的响应包装在流式/取消场景有历史坑；纯 ASGI 只在
请求进入时检查一次，响应（含 SSE 流）零开销透传。

放行范围（API_TOKEN_EXEMPT_PATHS）：
- /api/livez、/api/readyz：容器/网关的存活与就绪探针（不返回敏感信息；
  /api/health?deep=1 会暴露配置详情，仍受保护）；
- OPTIONS 预检：浏览器跨域预检不携带 Authorization（真正的请求仍会校验）。

令牌来源：`Authorization: Bearer <token>`（标准）或 `X-Zhiwen-Token: <token>`
（curl/脚本便捷）。比较用 hmac.compare_digest（常数时间）。
"""

from __future__ import annotations

import hmac

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

API_TOKEN_EXEMPT_PATHS = frozenset({"/api/livez", "/api/readyz"})


class ApiTokenMiddleware:
    """校验全局 API Token；未匹配返回 401（带 X-Zhiwen-Unauthorized 标记）。"""

    def __init__(
        self,
        app: ASGIApp,
        token: str,
        exempt_paths: frozenset[str] = API_TOKEN_EXEMPT_PATHS,
    ) -> None:
        self.app = app
        self.token = token.encode("utf-8")
        self.exempt_paths = frozenset(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if (
            not path.startswith("/api/")
            or path in self.exempt_paths
            or scope.get("method") == "OPTIONS"
        ):
            await self.app(scope, receive, send)
            return
        if self._token_ok(scope):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            status_code=401,
            content={"detail": "未授权：缺少或错误的 API Token"},
            headers={"X-Zhiwen-Unauthorized": "api-token"},
        )
        await response(scope, receive, send)

    def _token_ok(self, scope: Scope) -> bool:
        bearer: bytes | None = None
        custom: bytes | None = None
        for key, value in scope.get("headers") or []:
            if key == b"authorization":
                parts = value.split(None, 1)
                if len(parts) == 2 and parts[0].lower() == b"bearer":
                    bearer = parts[1].strip()
            elif key == b"x-zhiwen-token":
                custom = value.strip()
        candidate = bearer if bearer is not None else custom
        return candidate is not None and hmac.compare_digest(candidate, self.token)
