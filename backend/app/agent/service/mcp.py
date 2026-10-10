"""MCP 服务器接入：工具加载、状态查询、手动连接。

从 LangGraphAgentService 拆出（2026-10-09）：只做移动，未改逻辑。"""

from __future__ import annotations

import json
import logging
from sqlalchemy.orm import Session
from ...db import repository as repo
from ...runtime_config import effective_or


logger = logging.getLogger(__name__)


class McpMixin:
    def _mcp_cache_dir(self):
        """MCP 工具 schema 缓存目录（连接失败时靠它保持工具定义字节稳定）。"""
        return self.settings.data_dir / "mcp_schema"

    def mcp_tools(self, db: Session | None) -> list:
        """加载启用中的 MCP 服务器工具（连接失败自动降级跳过）。"""
        if not effective_or(self.settings, "mcp_enabled", True):
            return []
        if self._mcp is None:
            from ...mcp_manager import MCPManager

            self._mcp = MCPManager(cache_dir=self._mcp_cache_dir())
        servers = self._load_mcp_servers(db)
        if not servers:
            return []
        try:
            return self._mcp.configure(servers)
        except Exception as exc:
            logger.warning("MCP 工具配置失败：%s", exc)
            return []

    def mcp_status(self) -> dict:
        """MCP 服务器运行状态（设置页用；不触发连接）。"""
        if self._mcp is None:
            return {}
        try:
            return self._mcp.status()
        except Exception:
            return {}

    def connect_mcp_servers(self, db: Session | None, ids: list[str] | None = None) -> dict:
        """立即创建/连接 MCP 会话（面板「启动」按钮、保存配置后调用）。

        连接在后台线程进行、不阻塞请求；调用方随后轮询 mcp_status() 看结果。
        """
        if not effective_or(self.settings, "mcp_enabled", True):
            return {"ok": False, "error": "MCP 功能总开关未启用", "status": {}}
        if db is None:
            return {"ok": False, "error": "数据库不可用", "status": {}}
        if self._mcp is None:
            from ...mcp_manager import MCPManager

            self._mcp = MCPManager(cache_dir=self._mcp_cache_dir())
        servers = self._load_mcp_servers(db)
        try:
            self._mcp.configure(servers)
            started = self._mcp.warm(ids)
        except Exception as exc:
            logger.warning("MCP 手动连接失败：%s", exc)
            return {"ok": False, "error": str(exc), "status": {}}
        return {"ok": True, "started": started, "status": self._mcp.status()}

    @staticmethod
    def _load_mcp_servers(db: Session | None) -> list[dict]:
        if db is None:
            return []
        try:
            raw = repo.get_meta(db, "mcp_servers")
            if raw:
                data = json.loads(raw)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []
