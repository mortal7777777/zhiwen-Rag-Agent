"""MCP（Model Context Protocol）客户端管理器。

支持两种传输：
- stdio：本地子进程（如 npx/uvx 起的 MCP 服务器）；
- streamable HTTP：远程 SSE/HTTP MCP 服务器。

每个服务器运行在独立的 asyncio 事件循环线程中，工具调用同步返回；
连接失败自动降级（跳过该服务器），不影响主流程。

缓存前缀不变量：工具定义（名称/描述/参数 schema）是 provider 前缀缓存
的一部分，会话内字节变化会让整段前缀失效。因此连接失败/未就绪时**不
移除工具**，改用上次成功连接时落盘的 schema 生成同样的工具定义（调用
时才提示不可用），保证工具数组跨连接状态字节稳定。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

logger = logging.getLogger(__name__)

# 连接超时：npx 首次冷启动实测可达 70s+（@playwright/mcp），20s 必然超时；
# 连接在后台线程进行、不阻塞请求，超时给足即可。
DEFAULT_CONNECT_TIMEOUT = 120.0
# 断连后的重试冷却：距上次尝试超过该秒数，才在请求路径上再触发一次后台连接
RECONNECT_COOLDOWN = 60.0


def _server_signature(cfg: dict) -> str:
    """服务器配置签名：同 id 但命令/参数变了也要重建会话。"""
    return json.dumps(
        [
            str(cfg.get("type") or "stdio"),
            str(cfg.get("command") or ""),
            [str(a) for a in (cfg.get("args") or [])],
            str(cfg.get("url") or ""),
            str(cfg.get("name") or ""),
        ],
        ensure_ascii=False,
    )


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
        py_type, _ = _py_type(spec)
        desc = str(spec.get("description", ""))
        # 尊重 schema 的 default：Playwright MCP 部分字段声明 required 但带
        # default（如 console_messages.level、network_requests.static），服务端
        # 空参可用默认值；客户端按"必填"解析会误拦空参调用（参数缺失）。
        if name in required and "default" not in spec:
            fields[name] = (py_type, Field(..., description=desc))
        else:
            fields[name] = (
                py_type,
                Field(default=spec.get("default"), description=desc),
            )
    if not fields:
        return None
    return create_model("MCPToolArgs", **fields)


class MCPServerSession:
    """一个 MCP 服务器的常驻连接（独立事件循环线程）。"""

    def __init__(self, config: dict, cache_dir: Path | None = None) -> None:
        self.config = config
        self.id = str(config.get("id") or "")
        self.name = str(config.get("name") or self.id)
        self.config_sig = _server_signature(config)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session = None
        self._context = None
        self._lock = threading.Lock()
        self._connecting = False
        self._last_attempt = 0.0
        self.tools: list[dict] = []
        # 上次成功连接的 schema 缓存：连接失败时用它生成同样的工具定义，
        # 保证工具数组字节稳定（前缀缓存不变量）
        self.cached_tools: list[dict] = self._load_cached_tools()
        self.connected = False
        self.error: str | None = None

    # ---------------- schema 缓存 ----------------

    def _cache_path(self) -> Path | None:
        if self.cache_dir is None or not self.id:
            return None
        return self.cache_dir / f"{self.id}.json"

    def _load_cached_tools(self) -> list[dict]:
        path = self._cache_path()
        if path is None or not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            tools = data.get("tools") if isinstance(data, dict) else None
            return [
                t
                for t in (tools or [])
                if isinstance(t, dict) and t.get("name")
            ]
        except Exception as exc:
            logger.debug("MCP 工具缓存读取失败 %s：%s", path, exc)
            return []

    def _save_cached_tools(self) -> None:
        path = self._cache_path()
        if path is None or not self.tools:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"name": self.name, "tools": self.tools},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("MCP 工具缓存写入失败 %s：%s", path, exc)

    # ---------------- 连接 ----------------

    def start(self, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> bool:
        """启动连接线程并初始化会话；失败返回 False（降级为缓存 schema）。"""
        with self._lock:
            self._last_attempt = time.time()
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
            if self.connected:
                return True
            self.close()
            return False
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            logger.warning("MCP 服务器 %s 连接失败：%s", self.name, self.error)
            self.close()
            return False

    def start_background(self, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> bool:
        """后台线程发起连接（不阻塞请求）。已在连接中/已连通则跳过。"""
        with self._lock:
            if self.connected or self._connecting:
                return False
            self._connecting = True

        def _worker() -> None:
            try:
                self.start(timeout=timeout)
            finally:
                with self._lock:
                    self._connecting = False

        threading.Thread(
            target=_worker,
            daemon=True,
            name=f"mcp-connect-{self.id or self.name}",
        ).start()
        return True

    def should_connect(self) -> bool:
        """是否该（重新）发起连接：未连通、不在连接中、且过了冷却期。"""
        with self._lock:
            if self.connected or self._connecting:
                return False
            return time.time() - self._last_attempt >= RECONNECT_COOLDOWN

    def available_tools(self) -> list[dict]:
        """当前用于组装工具定义的 schema：已连通用实时，否则用上次缓存。"""
        return self.tools if self.connected else self.cached_tools

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
            prev_names = [t.get("name") for t in self.cached_tools]
            now_names = [t.get("name") for t in self.tools]
            if prev_names and prev_names != now_names:
                logger.warning(
                    "MCP 服务器 %s 工具集变化（%d -> %d 个）：缓存前缀会失效，"
                    "常见于服务器版本升级",
                    self.name,
                    len(prev_names),
                    len(now_names),
                )
            self._save_cached_tools()
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
        self._session = None
        self._context = None
        self._loop = None
        self._thread = None
        self.connected = False


class MCPManager:
    """管理一组 MCP 服务器，并把工具包装成 LangChain BaseTool。"""

    def __init__(
        self,
        cache_dir: Path | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self._servers: dict[str, MCPServerSession] = {}
        self._lock = threading.Lock()
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.connect_timeout = connect_timeout

    def configure(self, servers: list[dict]) -> list[BaseTool]:
        """按配置启停服务器，返回工具定义（未连通用上次 schema 兜底）。

        连接在后台线程进行、不阻塞请求；连接失败也不再丢工具——用缓存
        schema 生成同样的定义，保持工具数组字节稳定（前缀缓存不变量）。
        """
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
                if server is not None and server.config_sig != _server_signature(cfg):
                    # 同 id 但配置变了：重建会话
                    try:
                        server.close()
                    except Exception:
                        pass
                    self._servers.pop(sid, None)
                    server = None
                if server is None:
                    server = MCPServerSession(cfg, cache_dir=self.cache_dir)
                    self._servers[sid] = server
                if server.should_connect():
                    server.start_background(timeout=self.connect_timeout)
                for t in server.available_tools():
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


# Playwright MCP 的 schema 缺陷：部分工具 schema 声明可选/带默认，但
# 服务端运行时校验更严格（缺 null 报错 / 三选一必填），客户端侧按工具名
# 补默认值绕过（服务端为第三方，升级前在本地兜底）。其他工具如有同类
# 问题按同样方式追加。
_MCP_TOOL_DEFAULT_ARGS: dict[str, dict] = {
    "browser_snapshot": {
        "target": "body",
        "depth": 10,
        "boxes": False,
    },
    # browser_tabs：服务端 zod 校验拒绝显式 null（index/url 虽为可选）。
    # 空参/全 null 时补 index=0、url="" 占位（已实测 list/close/new 均接受）；
    # action="select" 时应显式传 index、action="new" 时应显式传 url
    # （枚举为 list/new/close/select）；显式非 None 值不会被覆盖。
    "browser_tabs": {
        "index": 0,
        "url": "",
    },
    # browser_wait_for：schema 全可选但服务端运行时要求 time/text/textGone
    # 至少其一（"Either time, text or textGone must be provided"），空参时
    # 补 1 秒等待兜底（已实测 {"time": 1} 被接受）。
    "browser_wait_for": {
        "time": 1,
    },
}


def _mcp_default_args(tool_name: str, kwargs: dict) -> dict:
    """为 MCP 工具补默认参数并剔除 null（仅当缺省时补，显式非 None 值不覆盖）。

    剔除 null 是所有工具通用的兜底：StructuredTool 经 pydantic 解析后可选
    字段会被填成 None（JSON null），而 Playwright MCP 服务端（zod .optional()）
    拒绝显式 null（报 "expected X, received null"），缺省则正常接受。
    """
    merged = dict(kwargs or {})
    defaults = _MCP_TOOL_DEFAULT_ARGS.get(tool_name) or {}
    for key, value in defaults.items():
        # 仅当显式传入了非 None 值才保留；值为 None（StructuredTool 经
        # pydantic 解析后可选字段常为 null）视为缺省，补默认值。
        if key.startswith("_"):
            continue
        if key in merged and merged[key] is not None:
            continue
        merged[key] = value
    if tool_name == "browser_snapshot" and not merged.get("filename"):
        # Playwright MCP 的 filename 允许任意文件名；自动命名避免覆盖旧快照
        merged["filename"] = f"snapshot-{int(time.time())}.md"
    return {k: v for k, v in merged.items() if v is not None}


def make_mcp_tool(
    server: MCPServerSession,
    server_cfg: dict,
    tool_name: str,
    description: str,
    input_schema: dict | None,
) -> BaseTool:
    """把 MCP 工具包装成 LangChain StructuredTool（同步调用）。"""

    def _invoke(**kwargs):
        if not server.connected:
            return {
                "summary": f"MCP[{server.name}] {tool_name} 暂不可用：服务器未连接",
                "error": (
                    f"MCP 服务器 {server.name} 当前未连接（连接中或不可达），"
                    "该工具暂不可用。请改用其他工具或方式完成任务，不要重复调用本工具。"
                ),
            }
        try:
            # 补默认参数：Playwright MCP 部分工具 schema 声明可选但服务端必填
            call_args = _mcp_default_args(tool_name, kwargs)
            output = server.call_tool(tool_name, call_args or {})
            return {
                "summary": f"MCP[{server.name}] {tool_name} 调用完成",
                "output": _truncate(output, 1500),
            }
        except Exception as exc:
            return {
                "summary": f"MCP[{server.name}] {tool_name} 调用失败：{exc}",
                "error": str(exc),
            }

    # 快照类工具:完整内容已在工具输出中(仅截断版),模型无需再读快照文件
    # (Playwright MCP 会把快照写到自己的目录,工作区内 read_file 读不到)
    extra_hint = (
        " 快照/页面内容已包含在本工具的输出中，直接使用输出即可，"
        "不要再尝试读取快照文件。"
        if tool_name == "browser_snapshot"
        else ""
    )
    return StructuredTool.from_function(
        func=_invoke,
        name=f"mcp_{tool_name}",
        description=(
            f"MCP 工具（服务器：{server.name}）：{description or tool_name}。"
            "当用户要求使用该能力、或任务需要该工具时调用。"
            f"{extra_hint}"
        ),
        args_schema=_schema_to_model(input_schema),
    )
