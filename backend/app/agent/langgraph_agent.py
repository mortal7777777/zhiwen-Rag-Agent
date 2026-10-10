"""LangGraphAgentService：LangGraph 编排的服务入口。

拆分历史：
- 2026-08-27：图定义、节点实现、工具辅助拆到 graph.py / nodes/ / utils.py / state.py；
- 2026-10-09：服务方法按职责拆到 service/ 下的 mixin（run / resume / tool_surface / mcp），
  本文件只保留**装配**：图与检查点的懒加载、设置刷新，以及 mixin 组合。

新增服务方法请放进 service/ 对应模块（主类行数上限有测试守着）。
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..rag.service import RAGService
from .agent import AgentService
from .graph import build_agent_graph
from .service.mcp import McpMixin
from .service.resume import ResumeMixin
from .service.run import RunMixin
from .service.tool_surface import ToolSurfaceMixin

logger = logging.getLogger(__name__)

class LangGraphAgentService(
    RunMixin,
    ResumeMixin,
    ToolSurfaceMixin,
    McpMixin,
    AgentService,
):
    """LangGraph 编排版 Agent 服务：接口与 AgentService 完全一致。"""

    def __init__(self, settings: Settings, rag_service: RAGService):
        super().__init__(settings, rag_service)
        self._graph = None
        self._mcp = None
        self._checkpoint_saver = None
        self._checkpoint_conn = None
        self.recursion_limit = getattr(settings, "agent_recursion_limit", 30)
        # conv_id -> (main, aux)：挂起前一段的用量快照，恢复线程 seed 后合并
        self._suspended_usage: dict = {}

    @property
    def graph(self):
        if self._graph is None:
            saver = self.checkpoint_saver
            self._graph = build_agent_graph(checkpointer=saver)
        return self._graph

    @property
    def checkpoint_saver(self):
        """原生 checkpointer（HITL 挂起/续跑依赖它）。

        sqlite 初始化失败时降级为内存版（同进程内功能完整，重启后挂起/续跑丢失），
        而不是返回 None——否则 interrupt 会直接崩、人工确认不可用。
        """
        if self._checkpoint_saver is None:
            try:
                from ..native_checkpoint import build_saver

                self._checkpoint_saver, self._checkpoint_conn = build_saver(
                    self.settings.meta_dir
                )
            except Exception as exc:
                logger.error(
                    "原生 checkpointer 初始化失败，降级为内存版（重启后挂起/续跑丢失）：%s",
                    exc,
                )
                from langgraph.checkpoint.memory import InMemorySaver

                self._checkpoint_saver = InMemorySaver()
        return self._checkpoint_saver

    def refresh(self) -> None:
        """设置变更后重置懒加载缓存；图结构不变，无需重建。

        不重置 MCP 管理器：连接由 MCPManager.configure() 按配置签名自行
        增删/重建，重建设置页保存会把所有 MCP 连接掐断重连（连接失败即
        工具数组变化 → 该模型全部会话的前缀缓存作废）。
        """
        super().refresh()


