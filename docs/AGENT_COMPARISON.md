# 与主流 Agent 应用的对比与差距分析

本文把本项目（个人知识库 RAG 智能助手）与三类主流 Agent 应用放在一起对比：
**Claude Code**（Anthropic，终端编程 Agent）、**Codex**（OpenAI，编程/通用 Agent）、
**Hermes**（Nous Research，开源 Agent 框架）。先对齐坐标系，再逐维度对比，
最后给出客观的不足清单与改进路线。

> **更新说明（2026-08-12）**：本文写作时列出的 P0 短板（文件/命令执行、MCP、
> checkpoint、AGENTS.md 文件记忆）后续已全部落地：受控执行工具 + HITL 审批、
> MCP client（stdio / streamable HTTP）、自定义 SQLite checkpoint、
> 文件型项目记忆均已实现；git 版本保护与 pytest（20 用例）也已补齐。
> 当前主要差距更新为：评测体系（RAGAS 类指标与回归门槛）、CI、
> LangGraph 原生 checkpointer 迁移、成本优化（见 §4 标注）。

## 0. 定位差异

| 系统 | 形态 | 定位 |
|---|---|---|
| 本项目 | Web 聊天（Vue 3）+ FastAPI | **对话型个人助手**：知识库问答、联网、记忆、视觉，本地优先 |
| Claude Code | 终端 TUI | **执行型编程 Agent**：改代码、跑命令、管理仓库 |
| Codex | CLI + 云端 + IDE | **执行型编程/通用 Agent**：代码任务、skills、会话恢复 |
| Hermes | 框架（TUI/Web/可接 ACP） | **通用 Agent 框架**：技能生态、多后端、自动化 |

三者偏"执行"，本项目偏"对话"。直接比功能不完全公平，但**编排、记忆、工具、
可观测性**的架构理念可以互相借鉴，这也是本文的重点。

## 1. 架构与功能对比

| 维度 | 本项目 | Claude Code | Codex | Hermes |
|---|---|---|---|---|
| 编排 | LangGraph 4 节点状态图（prepare/agent/tools/finalize） | 内部循环 + 子代理（subagents） | 内部编排器 + skills 按需注入 | Agent 抽象 + toolsets + ACP 多后端复用 |
| 状态/检查点 | AgentState + MySQL + 自定义 SQLite checkpoint（中断恢复） | checkpoint / 会话恢复 | session restore / checkpoint | hermes_state 可移植状态 |
| 工具 | KB 检索 / 联网 / 识图 / skill_lookup + 文件/命令受控工具（HITL） | 文件编辑 / Shell / 浏览 / MCP / Git | 文件 / Shell / 浏览 / MCP / sandbox | 大量内置工具 + MCP 市场 + 外部 skills |
| 技能 | 扫描本机 Codex/Claude/Hermes（146 个去重），启停/隐藏、结构化目录 | 无开放 skills 体系（内部子代理） | `~/.codex/skills` + 市场 + AGENTS.md | 80+ 内置 + 114 optional SKILL.md |
| 记忆 | 三层：滚动摘要（软窗口）/ 分类长期事实 / 画像摘要，MySQL | CLAUDE.md（静态 + 自动维护的项目记忆） | AGENTS.md + 会话历史 | trajectory 压缩 + state 移植 + routines |
| 上下文工程 | 软窗口双条件、模板差异化预算、记忆粗筛+语义召回、查询扩展判定 | 长任务上下文压缩、子任务隔离 | 上下文管理 + 多文件索引 | trajectory_compressor 中间态压缩 |
| 并行 | 串行 ReAct（无子代理） | subagents 并行子任务 | 并行任务（部分场景） | 多 agent / 多后端并行 |
| 可观测 | agent_runs + JSONL trace + /api/metrics + token 聚合 | verbose 日志、/status | usage 面板、session 列表 | 日志 + state 可导出 |
| UI | Web（主题/打字机/引用索引/设置面板/技能管理） | 终端 TUI | CLI + 云端 Web | TUI + Web |
| 安全 | 受控执行 + HITL 人工确认（无系统级沙箱） | 权限许可系统（命令需批准） | sandbox 容器 + 权限 | 沙箱/容器选项 |
| 扩展生态 | MCP client（stdio / streamable HTTP） | MCP 丰富 | MCP + skills 市场 | MCP + ACP + cron |
| 多用户 | 单用户本地 | 单机 | 单用户（云端多端） | 单机 |

## 2. 各系统机制要点

### Claude Code

- **CLAUDE.md**：项目级记忆文件，支持静态指令 + 运行中自动追加（用户确认），
  相当于"跨会话的项目长期记忆"；
- **subagents**：把大任务拆给多个子代理并行执行，再汇总；
- **权限系统**：危险命令/文件写入需用户批准，可持久化许可；
- **checkpoint / 恢复**：中断后可回到某个检查点继续；
- **hooks**：任务生命周期事件（start/stop）可触发脚本。

### Codex

- **AGENTS.md**：项目/仓库级指令文件，进入目录时自动加载；
- **skills**：`~/.codex/skills`（本机）+ 可发布/安装的 skill 市场；
  这也是本项目技能模块直接复用的来源之一；
- **session restore / checkpoint**：对话/任务可保存并恢复；
- **sandbox**：代码/命令在受控环境执行；
- **usage 面板**：用量与成本可视化。

### Hermes

- **Skills 生态**：`skills/` 与 `optional-skills/` 两级分类（本机扫描到 81 + 114 个
  SKILL.md），按需检索注入——本项目技能模块的思路同源；
- **ACP（Agent Client Protocol）**：可复用 Claude Code / Codex / OpenCode 等客户端；
- **state portability**：hermes_state 可导出/导入，跨设备迁移；
- **trajectory 压缩**：把长轨迹压缩成中间状态，解决超长会话；
- **routines / cron**：定时任务与自动化。

## 3. 本项目已具备、并不落后的部分

- **RAG 检索质量**：Parent-Child 切分、混合检索（kNN+BM25+RRF）、本地重排、
  查询扩展（含复杂度判定）、CRAG 兜底——比多数通用 Agent 的"搜索工具"更专业；
- **三层记忆**：滚动摘要（软窗口 + 模板差异化预算）、分类长期事实（显式"记住"信号 +
  自动整合 + 画像摘要）、按需语义召回——对话型助手中属于完整度较高的一档；
- **显式编排**：LangGraph 状态图、递归兜底、工具上限、计划-工具映射，结构比手工
  循环更可维护，且为后续检查点/并行留好了位置；
- **可观测**：每次运行的问题/计划/工具轨迹/耗时/token 用量入库 + JSONL trace +
  metrics，基础可观测齐备；
- **多供应商管理**：cc-switch 式对话/视觉供应商切换、脱敏保存、热生效；
- **技能复用**：能直接扫描并启用 Codex / Claude / Hermes 本机 skills，比自建技能
  体系省力；
- **Web UI**：主题、打字机、引用索引、设置面板、技能管理，体验高于终端型产品
  在"对话"场景下的呈现。

## 4. 客观不足（按影响排序；~~删除线~~ = 已落地）

> 2026-08-16 批量更新：此前多轮已把 P0/P1 大项落地，状态同步如下。

### P0 · 能力边界

1. ~~**无文件/命令级执行能力**~~ **已落地（2026-08-12）**：受控工具集 + HITL 审批、
   路径容错、失败不烧预算；~~仍无系统级沙箱~~ **Docker 沙箱已落地（2026-08-16）**：
   `--network=none`/只读根/限资源容器，CLI `--sandbox` 与 `/sandbox` 请求级一键切换。
2. ~~**无 MCP**~~ **已落地**：MCP client 支持 stdio / streamable HTTP；
   生态厚度（现成服务器配套、市场）仍不及主流。
3. ~~**无子代理/并行**~~ **已落地（2026-08-13）**：计划工具型步骤经 LangGraph
   `Send` fan-out 到独立上下文子代理（含 HITL），merge 重排全局引用编号。
4. ~~**无会话 checkpoint/恢复**~~ **已落地**：自定义 SQLite checkpoint +
   LangGraph 原生 saver（时间线审计/回滚）；2026-08-16 再加**消息级 rewind**
   （CLI `/rewind` + Web 回退按钮：删除后续消息并把原文放回输入框）。

### P1 · 记忆与可观测

5. ~~**无文件型项目记忆**~~ **已落地（2026-08-13）**：AGENTS.md 逐级向上合并 +
   用户级 `~/.myragagent/AGENTS.md`（类 CLAUDE.md）。
6. **上下文压缩无中间态**：只有"滚动摘要"（文本级），没有 trajectory 级压缩
   （Hermes trajectory_compressor 那种）——长任务中间推理会丢失。
   （2026-08-16 缓解：`/compact` 与 Web「上下文」面板支持手动压缩 +
   `/context` 占用可视化。）
7. ~~**可观测无 UI**~~ **部分落地（2026-08-16）**：运行记录页有分阶段耗时条
   （prepare/子代理/模型/工具/TTFT）+ 用量统计卡（tokens/命中率/估算成本/
   每日趋势）；仍缺 Langfuse/Grafana 级 trace 下钻。
8. **技能无沙箱执行**：skills 目前只是"注入指令"，shell/文件类技能无法可靠执行；
   要真正跑起来需要执行沙箱 + 权限确认。
9. **无多用户/权限隔离**：单用户本地，无 user_id 隔离，不适合多人部署。
   （2026-08-16 与用户讨论定稿：**不做**——进程内单例审批/MCP 状态与本地
   CUDA 模型都是单用户假设；将来若要共用走"多配置文件"路线，不引入租户体系。

### P2 · 工程化

10. ~~**无用量/成本仪表盘**~~ **已落地（2026-08-16）**：`/runs/stats` +
    RunsView 统计卡（tokens/命中率/估算成本/每日趋势）；CLI headless
    `-p --output-format json` 可接脚本/CI。
11. **无自动化/cron**：Hermes 有 routines，本项目没有定时任务。
12. ~~**无 hooks/事件系统**~~ **已落地（2026-08-13）**：PreToolUse/PostToolUse
    用户脚本回调（deny 拦截 / additional_context 回填）。
13. ~~**评测与 CI（2026-08-12 新增）**~~ **大部分落地**：pytest（82 用例）+
    pre-commit + RAGAS 基线（faithfulness≈0.975）+ `scripts/regression_gate.py`
    回归门槛（2026-08-16）；仍无定时 CI 自动跑门槛。

## 5. 改进路线（每条给出落地路径）

| 优先级 | 改进 | 落地路径 |
|---|---|---|
| P0 | 接入 MCP | 新增 `mcp_client` 模块，把 MCP 工具用 LangChain `@tool` 包装注册进 `build_tools`；设置页加 MCP 服务器配置（SSE/stdio） |
| P0 | 会话 checkpoint | LangGraph `MemorySaver`/`AsyncSqliteSaver`，State 接入 `conversation_id`，`/api/conversations` 支持恢复中间状态 |
| P0 | 子代理/并行 | LangGraph 并行节点：多路检索（KB+Web）并行、长任务拆子图；复用现有工具闭包 |
| P0 | 受控执行工具 | 新增 `file_tool`（白名单目录读写）与 `command_tool`（Docker 或子进程 + 超时 + 白名单），前端加"执行确认" |
| P1 | 文件型项目记忆 | 会话结束/手动触发时把画像摘要导出为 `AGENTS.md`（可配置路径），下次会话自动加载 |
| P1 | trajectory 压缩 | 基于现有 JSONL trace 做"分段摘要 + 状态恢复"任务（LLM 压缩中间轮次） |
| P1 | Langfuse UI | 安装 langfuse（Docker 或 Python 包），在 `tracing.py` 的 `write_trace` 加分支推送，前端/独立页面看 trace |
| P1 | 技能执行沙箱 | skills 增加 `executable` 元数据 + 沙箱执行器 + 权限确认流 |
| P1 | 多用户隔离 | 表加 `user_id`，API 按用户过滤；登录可后置 |
| P2 | 用量仪表盘 | 前端加"用量"页，读 `/api/runs` token 聚合 |
| P2 | cron/routines | 轻量调度器（apscheduler）+ 模板化任务 |
| P2 | hooks | 复用 LangGraph 回调/中间事件，注册 webhook |

## 6. 思维链与决策透明性

- **现状**：模型在工具调用前会输出简短意图（prompt 要求），前端展示
  **执行计划 + 工具轨迹 + 引用来源**；原始思维链（`reasoning_content`）不采集、
  不展示。当前模型（deepseek-v4-flash）的推理 token 极少（单轮几十个），
  且 API 未暴露 `reasoning_content` 字段。
- **建议**：不要直接展示原始 CoT——存在泄露提示词/知识库内容、噪音大、
  模型安全共识不鼓励的问题。可借鉴 DeepSeek 网页端做**"思考摘要"折叠块**：
  模型自述"我打算先检索…再对比…"的简短过程说明，与现有 plan/tool_trace 互补；
  trace 中已记录推理 token 数，可做统计。

## 7. 结论

本项目在**对话型助手**这个定位上，检索质量、记忆体系、编排结构、可观测性
已经达到不错的完成度，部分维度（RAG、三层记忆、Web UI）甚至强于通用 Agent。
与主流执行型 Agent 的主要差距集中在**执行能力与生态**：文件/命令执行、MCP、
子代理、checkpoint、可视化 trace。建议按 P0 → P1 → P2 的顺序补齐：
先接 MCP 和 LangGraph checkpoint（改动小、收益大），再做受控执行工具与子代理，
随后补 Langfuse 可视化与文件型记忆。
