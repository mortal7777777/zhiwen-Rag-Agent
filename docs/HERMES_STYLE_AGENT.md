# 向 Hermes 式 Agent 演进：终端 / Web / 桌面 的架构原理与路线

目标：把本项目从"Web 对话型助手"扩展成与 Claude Code / Codex / Hermes 同类——
可以在**终端里对话、读写文件、写代码、执行命令**，并可以构建成桌面应用。
本文先讲清主流 Agent 的三种前端形态与底层原理，再给本项目的演进路线。

## 1. 主流 Agent 的三种前端形态与共同原理

Claude Code / Codex / Hermes 表面上是不同的产品，内核是同一套分层：

```text
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  TUI（终端）  │   │  Web UI      │   │  桌面应用壳   │
└──────┬───────┘   └──────┬───────┘   └──────┬───────┘
       │  键盘事件/渲染      │  HTTP/SSE/WS     │  内嵌 WebView/后端
       ▼                   ▼                 ▼
┌─────────────────────────────────────────────────────┐
│            Agent 运行时（核心，与前端解耦）            │
│   事件循环 / 状态 / 工具集（文件/终端/搜索/MCP/技能）   │
│   记忆 / 上下文压缩 / 规划 / ReAct / 子代理           │
└─────────────────────────────────────────────────────┘
```

### 终端 TUI 是怎么实现的

- **渲染层**：Python 用 Rich / Textual，Node 用 Ink。它们本质是"把屏幕当画布"：
  每帧重绘文本界面，处理键盘/鼠标事件循环（如 Textual 的 `on_key`、`on_click`）；
- **通信层**：TUI 与 agent 进程之间走 **stdio JSON-RPC**（或 Hermes 的 **ACP** 协议）：
  客户端往 stdin 写 `{"type":"prompt","content":"..."}`，agent 往 stdout 回
  `{"type":"stream_event","content":...}`、`{"type":"tool_use",...}` 等事件；
- **所以"终端直接启动"的原理**是：一个进程负责 agent 逻辑，一个进程（或同进程的
  TUI 视图）负责界面，二者通过标准输入输出交换 JSON 消息。

### Hermes 的 ACP 与"一个 agent 能被多个客户端驱动"

Hermes 提供了 **ACP（Agent Client Protocol）适配器**：agent 侧实现 ACP 服务器，
Claude Code / Codex / OpenCode 等本就支持 ACP 的客户端就能直接驱动它。
好处：你写一次 agent 逻辑，终端、IDE、网页都可以用，还能复用别人写好的客户端。
我们扫描到的 `D:\agents\hermes\acp_adapter`、`acp_registry` 就是这套东西。

### Web UI / 桌面应用

- **Web**：浏览器 → HTTP/SSE/WebSocket → agent 运行时（本项目已经是这种形态，
  只是 agent 是对话型、工具集偏 RAG）；
- **桌面应用**：本质是"本地 Web 服务 + 壳"：
  - **PWA**：把现有前端加 `vite-plugin-pwa`，可安装到桌面/手机，离线缓存；
  - **Tauri**：Rust 壳内嵌 WebView，启动时拉起后端子进程（或后端独立跑），
    体积小、内存低，是当前推荐方向；
  - Electron：能力相同但体积大，不推荐。

## 2. 本项目现状与差距

| 能力 | 现状 |
|---|---|
| Agent 运行时 | ✅ LangGraph 状态图（prepare/agent/tools/finalize）+ 记忆/计划/引用溯源 |
| Web 前端 | ✅ Vue 3 + SSE（主题/打字机/设置面板/运行记录） |
| 文件/命令工具 | ✅ `file_tool` / `command_tool`（白名单 + 超时 + 确认） |
| 技能 | ✅ 扫描 Codex/Claude/Hermes skills，结构化目录 |
| 终端对话 | ✅ 完整 CLI（`myragagent` 命令）：/new /tools /todos /memory 等命令菜单、审批弹窗（数字键 1/2/3）、Markdown 渲染、Tab 补全、Ctrl+C 打断、历史持久化、按目录加载 AGENTS.md、task_mode |
| ACP 桥 | ❌ 无（不能用 Claude Code / Codex 客户端驱动本 agent） |
| TUI | ⚠️ 终端 CLI 已类 TUI（prompt_toolkit），未上 Textual/Ink 框架 |
| 桌面应用 | ❌ 无（未做 PWA/Tauri 打包） |
| 子代理并行 | ✅ 计划内工具型步骤经 LangGraph `Send` fan-out 到独立上下文子代理，merge 汇总（详见 docs/ORCHESTRATION.md 场景 B） |

## 3. 演进路线图

### 阶段一：最小终端客户端（✅ 已完成）
`backend/cli_agent.py`：纯标准库，连 `/api/agent/stream`，支持多轮会话、
工具事件展示、`/new`、`/tools` 切换、Ctrl+C 停止。这是"终端里和 agent 对话"
的最简形态，也验证了"前端只是壳，agent 核心复用"的原则。

### 阶段二：真正的 TUI（推荐 Textual）
- 交互：快捷键（Enter 发送、Ctrl+C 停止、Tab 补全）、消息列表 + 输入框双栏、
  工具调用折叠面板、代码块高亮；
- 复用：直接调现有 `/api/agent/stream`，渲染层换成 Textual；
- 成本：一个 `tui_client.py`（~400 行），无后端改动。

### 阶段三：ACP 桥（复用 Claude Code / Codex 客户端）
- 新增 `app/acp_server.py`：实现 ACP 协议（stdio JSON-RPC），把 LangGraph agent
  的 `run` 事件流映射成 ACP 的 `stream_event` / `tool_use` 消息；
- 之后 `claude` / `codex`（支持 ACP 的版本）可以直接驱动本 agent，
  终端体验直接对齐 Claude Code；
- 参考 `D:\agents\hermes\acp_adapter` 的实现思路。

### 阶段四：桌面应用
- **第一步（低成本）**：`vite-plugin-pwa`，现有前端可安装到桌面/手机；
- **第二步**：Tauri 壳：本地启动后端子进程 + WebView 加载前端，
  打包成单机应用（Windows 下约 10~20MB）；
- 后端已支持 `--host 127.0.0.1` 独立进程，Tauri 只需负责拉起与关停。

### 阶段五：执行与并行能力对齐 Claude Code
- 已有：`command_tool`（白名单+超时）、`file_tool`（目录内读写）、
  `grep_search`（代码搜索）、Playwright 浏览器工具（MCP）、
  Send 子代理并行（独立上下文 + HITL + merge 汇总）；
- 待补：Git 操作工具、TUI（Textual）。

## 4. 关于"在代码块上执行"与"在终端执行"的区别

- 代码块按钮 = 便捷入口（复用 command_tool），适合偶尔跑一段；
- 真正像 Claude Code 的体验 = agent 自己决定"读文件 → 改代码 → 跑测试 → 看输出"
  的完整循环，这依赖：文件工具 + 命令工具 + 足够强的工具选择策略 +
  终端/TUI 展示。两者不冲突：前者是入口，后者是能力闭环。

## 5. 建议

按阶段二（Textual TUI）→ 阶段三（ACP 桥）→ 阶段四（PWA → Tauri）推进。
每一步都能独立交付、可演示，且后端几乎不用大改——这正是"前端壳与 agent 核心
解耦"带来的红利。
