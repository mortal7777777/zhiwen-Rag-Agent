"""`LangGraphAgentService` 的按职责拆分（2026-10-09 物理拆分）。

拆法：mixin 组合，而不是把状态拆出去——服务的状态（图、检查点、
`_suspended_usage`、`_mcp`）仍由主类持有，方法按职责分层：

    service/base.py          ServiceBaseMixin   图执行参数（预算↔步数、线程配置）
    service/run.py           RunMixin           一次问答：invoke、收尾、超时看门狗
    service/resume.py        ResumeMixin        挂起恢复/崩溃续跑/丢弃运行
    service/tool_surface.py  ToolSurfaceMixin   工具面装配（缓存前缀键的一部分）
    service/mcp.py           McpMixin           MCP 服务器接入

约束：mixin 之间不互相调用（只允许依赖 base）；新增方法请放进对应职责的
模块，而不是主类（主类的行数上限有测试守着）。
"""
