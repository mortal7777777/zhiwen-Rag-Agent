"""MCP 工具 schema 缓存与降级单测：连接失败不丢工具、定义字节稳定。

核心不变量：断连时用缓存 schema 生成的工具定义，其前缀哈希必须与
连通时完全一致——否则工具数组变化会让 provider 前缀缓存整段失效。
"""

from __future__ import annotations

import json
import time

from app.agent.utils import _tools_prefix_hash
from app.mcp_manager import (
    MCPManager,
    MCPServerSession,
    _server_signature,
    make_mcp_tool,
)

CFG = {
    "id": "demo",
    "name": "演示服务",
    "type": "stdio",
    "command": "noop",
    "args": ["--x"],
    "enabled": True,
}
SCHEMA = [
    {
        "name": "browser_navigate",
        "description": "打开网页",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "地址"}},
            "required": ["url"],
        },
    },
    {
        "name": "browser_snapshot",
        "description": "页面快照",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _build_tools(session: MCPServerSession, tools: list[dict]):
    return [
        make_mcp_tool(session, session.config, t["name"], t["description"], t["inputSchema"])
        for t in tools
    ]


def test_schema_cache_roundtrip(tmp_path):
    live = MCPServerSession(CFG, cache_dir=tmp_path)
    live.tools = SCHEMA
    live._save_cached_tools()

    path = tmp_path / "demo.json"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["tools"] == SCHEMA

    reborn = MCPServerSession(CFG, cache_dir=tmp_path)
    assert reborn.cached_tools == SCHEMA
    assert reborn.connected is False


def test_cached_tools_keep_prefix_hash_identical(tmp_path):
    """断连（用缓存 schema）与连通（用实时 schema）的工具定义哈希必须一致。"""
    live = MCPServerSession(CFG, cache_dir=tmp_path)
    live.tools = SCHEMA
    live.connected = True
    live._save_cached_tools()
    hash_live = _tools_prefix_hash(_build_tools(live, live.tools))

    dead = MCPServerSession(CFG, cache_dir=tmp_path)  # 读缓存，未连接
    assert dead.connected is False
    hash_cached = _tools_prefix_hash(_build_tools(dead, dead.available_tools()))

    assert hash_live == hash_cached


def test_disconnected_tool_call_returns_error(tmp_path):
    dead = MCPServerSession(CFG, cache_dir=tmp_path)
    dead.cached_tools = SCHEMA
    tool = _build_tools(dead, dead.available_tools())[0]
    result = tool.invoke({"url": "https://example.com"})
    assert "未连接" in result["error"]
    assert "暂不可用" in result["summary"]


def test_configure_uses_cache_without_blocking(tmp_path, monkeypatch):
    """连接不阻塞：configure 立即返回缓存工具，连接交给后台。"""
    live = MCPServerSession(CFG, cache_dir=tmp_path)
    live.tools = SCHEMA
    live._save_cached_tools()

    spawned: list[str] = []
    monkeypatch.setattr(
        MCPServerSession,
        "start_background",
        lambda self, timeout=120.0: spawned.append(self.id) or True,
    )

    mgr = MCPManager(cache_dir=tmp_path)
    t0 = time.time()
    tools = mgr.configure([CFG])
    assert time.time() - t0 < 1.0  # 不等待连接
    assert spawned == ["demo"]
    assert [t.name for t in tools] == ["mcp_browser_navigate", "mcp_browser_snapshot"]

    # 冷却期内不重复触发连接
    mgr._servers["demo"]._last_attempt = time.time()
    assert mgr._servers["demo"].should_connect() is False
    mgr.configure([CFG])
    assert spawned == ["demo"]


def test_configure_rebuilds_on_config_change(tmp_path, monkeypatch):
    """同 id 但命令/参数变了 → 重建会话（旧连接不再复用）。"""
    monkeypatch.setattr(
        MCPServerSession,
        "start_background",
        lambda self, timeout=120.0: True,
    )
    mgr = MCPManager(cache_dir=tmp_path)
    mgr.configure([CFG])
    first = mgr._servers["demo"]

    changed = dict(CFG, args=["--y"])
    assert _server_signature(changed) != _server_signature(CFG)
    mgr.configure([changed])
    second = mgr._servers["demo"]
    assert second is not first
    assert second.config["args"] == ["--y"]


def test_disabled_server_has_no_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(
        MCPServerSession, "start_background", lambda self, timeout=120.0: True
    )
    mgr = MCPManager(cache_dir=tmp_path)
    assert mgr.configure([dict(CFG, enabled=False)]) == []
