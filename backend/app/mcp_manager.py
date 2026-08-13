"""MCP（Model Context Protocol）客户端管理器。

支持两种传输：
- stdio：本地子进程（如 npx/uvx 起的 MCP 服务器）；
- streamable HTTP：远程 SSE/HTTP MCP 服务器。

每个服务器运行在独立的 asyncio 事件循环线程中，工具调用同步返回；
连接失败自动降级（跳过该服务器），不影响主流程。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

logger = logging.getLogger(__name__)


def _truncate(text: str, limit: int = 900) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _schema_to_model(input_schema: dict | None):
    """把 MCP 工具的 JSON Schema 转成 pydantic 参数模型（支持常用基础类型）。"""
    from pydantic import BaseModel, Field, create_model

    if not input_schema:
        return None
    properties = input_schema.get("properties") or {}
    required = set(input_schema.get("required") or [])
    fields: dict[str, Any] = {}

    def _py_type(spec: dict):
        t = spec.get("type", "string")
        if t == "integer":
            return (int, Field(...))
        if t == "number":
            return (float, Field(...))
        if t == "boolean":
            return (bool, Field(...))
        if t == "array":
            return (list, Field(...))
        if t == "object":
            return (dict, Field(...))
        return (str, Field(...))

    for name, spec in properties.items():
        py_type, default_field = _py_type(spec)
        if name in required:
            fields[name] = (py_type, Field(..., description=str(spec.get("description", ""))))
        else:
            fields[name] = (
                py_type,
                Field(default=None, description=str(spec.get("description", ""))),
            )
    if not fields:
        return None
    return create_model("MCPToolArgs", **fields)


class MCPServerSession:
    """一个 MCP 服务器的常驻连接（独立事件循环线程）。"""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.id = str(config.get("id") or "")
        self.name = str(config.get("name") or self.id)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session = None
        self._context = None
        self.tools: list[dict] = []
        self.connected = False
        self.error: str | None = None

    def start(self, timeout: float = 20.0) -> bool:
        """启动连接线程并初始化会话；失败返回 False（降级跳过）。"""
        try:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_loop,
                daemon=True,
                name=f"mcp-{self.name}",
            )
            self._thread.start()
            fut = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
            fut.result(timeout=timeout)
            return self.connected
        except Exception as exc:
            self.error = str(exc)
            logger.warning("MCP 服务器 %s 连接失败：%s", self.name, exc)
            return False

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _connect(self) -> None:
        try:
            from mcp import ClientSession

            read = write = None
            if self.config.get("type") == "http":
                from mcp.client.streamable_http import streamable_http_client

                url = self.config.get("url") or ""
                if not url:
                    self.error = "缺少 URL"
                    return
                self._context = streamable_http_client(url)
                read, write = await self._context.__aenter__()
            else:
                from mcp.client.stdio import StdioServerParameters, stdio_client

                command = self.config.get("command") or ""
                args = [str(a) for a in (self.config.get("args") or [])]
                params = StdioServerParameters(command=command, args=args)
                self._context = stdio_client(params)
                read, write = await self._context.__aenter__()
            self._session = ClientSession(read, write)
            await self._session.__aenter__()
            try:
                await self._session.initialize()
            except Exception:
                pass  # 某些实现进入上下文时已初始化
            result = await self._session.list_tools()
            self.tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": (
                        getattr(t, "inputSchema", None)
                        or getattr(t, "input_schema", None)
                    ),
                }
                for t in result.tools
            ]
            self.connected = True
            logger.info("MCP 服务器 %s 就绪，%d 个工具", self.name, len(self.tools))
        except Exception as exc:
            self.error = str(exc)
            logger.warning("MCP 初始化 %s 失败：%s", self.name, exc)

    def call_tool(self, name: str, arguments: dict, timeout: float = 90.0) -> str:
        if not self.connected or self._session is None or self._loop is None:
            raise RuntimeError(f"MCP 服务器 {self.name} 未连接：{self.error or '未知'}")
        fut = asyncio.run_coroutine_threadsafe(
            self._call_tool(name, arguments),
            self._loop,
        )
        return fut.result(timeout=timeout)

    async def _call_tool(self, name: str, arguments: dict) -> str:
        result = await self._session.call_tool(name, arguments)
        parts = []
        for content in result.content or []:
            text = getattr(content, "text", None)
            if text:
                parts.append(str(text))
        if parts:
            return "\n".join(parts)
        return json.dumps(result.model_dump(), ensure_ascii=False)[:2000]

    def close(self) -> None:
        if self._loop and self._thread and self._loop.is_running():
            try:
                async def _shutdown():
                    if self._session is not None:
                        try:
                            await self._session.__aexit__(None, None, None)
                        except Exception:
                            pass
                    if self._context is not None:
                        try:
                            await self._context.__aexit__(None, None, None)
                        except Exception:
                            pass

                asyncio.run_coroutine_threadsafe(_shutdown(), self._loop).result(timeout=5)
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass


class MCPManager:
    """管理一组 MCP 服务器，并把工具包装成 LangChain BaseTool。"""

    def __init__(self) -> None:
        self._servers: dict[str, MCPServerSession] = {}
        self._lock = threading.Lock()

    def configure(self, servers: list[dict]) -> list[BaseTool]:
        """按配置启停服务器，返回所有可用工具的 LangChain 包装。"""
        with self._lock:
            enabled = {
                str(s.get("id"))
                for s in servers
                if s.get("enabled") and (s.get("command") or s.get("url"))
            }
            # 关闭已删除/禁用/变更的服务器
            for sid in list(self._servers):
                if sid not in enabled:
                    try:
                        self._servers[sid].close()
                    except Exception:
                        pass
                    self._servers.pop(sid, None)
            tools: list[BaseTool] = []
            for cfg in servers:
                sid = str(cfg.get("id") or "")
                if sid not in enabled:
                    continue
                server = self._servers.get(sid)
                if server is None:
                    server = MCPServerSession(cfg)
                    server.start()
                    self._servers[sid] = server
                if not server.connected:
                    continue
                for t in server.tools:
                    tools.append(
                        make_mcp_tool(server, cfg, t["name"], t["description"], t["inputSchema"])
                    )
            return tools

    def shutdown(self) -> None:
        with self._lock:
            for server in self._servers.values():
                try:
                    server.close()
                except Exception:
                    pass
            self._servers.clear()


def make_mcp_tool(
    server: MCPServerSession,
    server_cfg: dict,
    tool_name: str,
    description: str,
    input_schema: dict | None,
) -> BaseTool:
    """把 MCP 工具包装成 LangChain StructuredTool（同步调用）。"""

    def _invoke(**kwargs):
        try:
            output = server.call_tool(tool_name, kwargs or {})
            return {
                "summary": f"MCP[{server.name}] {tool_name} 调用完成",
                "output": _truncate(output, 1500),
            }
        except Exception as exc:
            return {
                "summary": f"MCP[{server.name}] {tool_name} 调用失败：{exc}",
                "error": str(exc),
            }

    return StructuredTool.from_function(
        func=_invoke,
        name=f"mcp_{tool_name}",
        description=(
            f"MCP 工具（服务器：{server.name}）：{description or tool_name}。"
            "当用户要求使用该能力、或任务需要该工具时调用。"
        ),
        args_schema=_schema_to_model(input_schema),
    )
