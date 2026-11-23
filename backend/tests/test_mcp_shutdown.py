"""AgentService 退出时必须关闭 MCP 会话（stdio 子进程不随父进程回收）。"""

from __future__ import annotations

from app.agent.service.mcp import McpMixin


class _Service(McpMixin):
    pass


class _FakeManager:
    def __init__(self):
        self.called = False

    def shutdown(self) -> None:
        self.called = True


def test_shutdown_closes_manager():
    service = _Service()
    fake = _FakeManager()
    service._mcp = fake
    service.shutdown()
    assert fake.called is True


def test_shutdown_without_manager_is_noop():
    service = _Service()
    service._mcp = None
    service.shutdown()
