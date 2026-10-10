"""终端客户端的内部分层（`cli_agent.py` 是入口与兼容层）。

分层（依赖单向、只能向下，2026-10-08 物理拆分）：

    cli_agent.py    REPL 主循环、流式渲染编排、命令分发、参数解析
      ├─ cli.http    后端 HTTP 调用 + 本地会话/历史文件
      ├─ cli.input   按键读取/行编辑/补全（含 Interrupted 等控制异常）
      ├─ cli.format  Markdown 渲染与数值格式化
      └─ cli.term    ANSI/宽度/裁剪等终端原语

约束：`cli.term` 不依赖其它层；`cli.format`/`cli.input` 只依赖 term；
`cli.http` 依赖 term+format。新增模块请沿用这个方向，避免环。
"""
