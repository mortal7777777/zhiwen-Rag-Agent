# LangGraph 编排逻辑与优化分析

## 当前编排（backend/app/agent/langgraph_agent.py）

```text
START → prepare → [fanout] → agent ⇄ tools → finalize → END
```

### prepare（每轮开头）

1. 会话/历史/滚动摘要 → 长期记忆召回 → 视觉识别 → 规划（仅复杂问题）；
2. 计划播种成 TodoWrite 任务清单（跨轮刷新，避免串台）；
3. 组装系统提示词（模板 + 工具纪律 + 技能目录 + 知识库文档清单），
   并注入“当前问题最相关的 Top2 技能”；
4. 组装分层消息（系统提示词 → 时间/项目记忆 → 摘要 → 记忆 → 图片 → 历史 → 问题）；
5. 按开关组装工具：KB / 联网 / 视觉 / 技能 / MCP / 文件命令 / todo_update。

### fanout（条件节点，新增）

知识库与联网同时开启、且计划同时需要两者时，并行跑
`knowledge_base_search + web_search`，结果合并回消息，再进入 agent。
优点：两路检索的墙钟时间从串行变成并行；缺点：每次都多花一次检索成本，
因此只在“计划明确需要两路”时触发。

### agent

LLM 流式生成；有 tool_calls → tools，无则收尾。计划硬约束：还有未完成的
工具型步骤时，路由推回 agent 继续执行（最多 3 次提示）。达工具上限/连续失败
上限时强制收尾。

### tools

批量并行执行工具（ThreadPoolExecutor），回填 ToolMessage；敏感操作 HITL 阻塞
等待审批；失败不烧迭代预算；执行后同步任务清单并广播 todos/plan_progress。

### finalize

来源去重 → 补完成纯推理步骤 → 持久化消息/运行记录/trace → 轨迹压缩 →
标题/plan_progress 补发 → done。

## 按任务场景分析瓶颈与优化

### 场景 A：单轮知识库问答

瓶颈：查询扩展的 LLM 调用（平均 19s）+ 每个扩展查询各跑一遍混合检索。

已做：扩展三路并行、同问题缓存、上限 4 条、CRAG 只在联网模式兜底。

下一步：
1. 简单问题短路（`_is_simple` 已经跳过 Multi-Query/HyDE，阈值可再放宽）；
2. 用弱模型做扩展/规划（省钱的辅助调用走便宜供应商）；
3. 检索向量缓存（skill/记忆/查询向量）。

### 场景 B：多步 Agent 任务（研究简报类）

瓶颈：agent → tools 串行多轮，每轮一次 LLM API 往返（3~8s），
计划 4 步就至少 5~7 轮。

已做：同轮多工具批量并行；fanout 并行两路检索。

已实现（2026-08-13）：计划里每个“工具型步骤”经 `Send` 派发给独立上下文的
`subagent` 节点（只暴露与该步骤相关的只读/检索工具，最多 2 轮 + 兜底总结），
并行执行后由 `merge` 节点合并结论摘要、重编号来源、回填工具轨迹并同步任务清单。
实测知识库+联网两分支并行返回结论，主 Agent 不再重复搜索。

已实现（2026-08-13 续）：文件/命令类子代理现在也带上写/编辑/删除/bash 工具，
敏感操作走与主 Agent 一致的 HITL（审批卡会标注“子代理等待确认”）；子代理轮数
可配（`agent_subagent_max_rounds`，默认 2）；写/改成功后可选自动运行
`verify_command` 并回填验证结果。

仍可加强：子代理结果去重与冲突说明、按任务难度动态决定是否派子代理、
写操作审批的“并行队列”体验（当前多张审批卡可分别点击，键盘快捷只聚焦第一张）。

### 场景 C：编程任务（写脚本 + 跑通验证）

瓶颈：现在是“模型写 → 用户批准 → 沙箱跑 → 模型看输出”，验证闭环依赖模型自觉。

已实现：写/改文件后自动验证并闭环修复：
- `verify_command` 显式配置优先；未配置时按扩展名自动检测
  （.py→py_compile / .js→node --check / .json、.yaml 语法解析），
  自动检测可用 `VERIFY_AUTO_DETECT` 关闭；
- 验证失败时把错误回填给模型并引导修复（edit_file/write_file 后自动复验），
  `VERIFY_MAX_RETRIES`（默认 1）控制最多修复复验次数，超过则要求如实说明
  原因、影响与建议，避免无限循环烧预算；
- 沙箱已可用：设置中把“命令沙箱”切到 docker（`command_sandbox=docker`），
  验证命令同样在 python:3.11-slim 容器里执行，`--network=none` 隔离外网。

已实现（2026-08-13 续 2）：Prompt caching 前缀缓存友好——系统提示词拆为
静态核心（模板+工具规则，放消息最前）+ 动态部分（任务清单/文档清单/技能目录，
放历史之后），静态核心与历史跨轮字节级稳定，命中 DeepSeek 等自动前缀缓存，
多轮工具循环的重复输入成本大幅下降；token 记录新增 cache_hit/cache_miss
（`usage_summary` 与 agent_runs 可见，可验证缓存命中率）。

### 场景 D：浏览器任务

已接入 Playwright MCP（browser_navigate / browser_type / browser_screenshot 等），
当前由 agent 按需调用。

下一步：把浏览器纳入 fanout 的第三路（KB+联网+浏览器并行），
并把截图结果作为视觉上下文注入；多步骤网页操作建议做成子代理避免污染主上下文。

### 场景 E：长任务 / 中断恢复

当前是自写 SQLite checkpoint（含 todos/plan/tool_trace）。下一步迁移
LangGraph 原生 checkpointer（`langgraph-checkpoint-sqlite`），
获得时间旅行/回滚；自写状态继续作为业务态保留。

## 演进目标图

```text
START → prepare → fanout(KB|Web|Browser|Code, 并行)
      → agent ⇄ tools → verify(自动测试/校验)
      → summarizer(子代理汇总) → finalize → END
```

原则：单轮任务走短路，多轮任务按计划派子代理，副作用任务必须有 verify，
敏感副作用始终 HITL。

## 复现 Claude Code 的编排（LangGraph 版）

> 依据公开流传/逆向整理的 Claude Code 实现（系统提示词、工具与权限模型、
> hooks、subagents 的设计），以下为架构层面的对照与复现方案。

Claude Code 的核心是一个 **agentic loop**：

```text
加载 CLAUDE.md + 系统提示词 + 工具定义
  → LLM 输出（文本 + 若干 tool_use）
  → 逐工具执行：先过权限规则（allow/ask/deny）与 PreToolUse hook
  → ToolResult 回填 + PostToolUse hook
  → 判断是否结束 / 是否超长需压缩 → 继续下一轮
Task 工具 = 派生 subagent（独立上下文、受限工具集），完成后把摘要回填
```

本项目与它的对应关系：

| Claude Code | 本项目现状 | 下一步复现 |
|---|---|---|
| 主循环 | `agent ⇄ tools` 状态图 | 已等价 |
| 权限规则 allow/ask/deny | `permissions.py` HITL + 白名单 | 已等价 |
| hooks（Pre/PostToolUse） | 无 | 在 tools 节点前后加 hook 注册表 |
| Task subagents | 无 | 用 `Send` 做 fan-out/fan-in 子图 |
| CLAUDE.md | AGENTS.md 项目记忆 | 已等价 |
| 上下文压缩 | 滚动摘要 + 轨迹压缩 | 已等价 |
| 沙箱 | Docker bash + 文件工具 | 已落地，继续加 verify |

### Send 子代理的 LangGraph 骨架

```python
def dispatch(state):
    # 主 agent 产出 Task(tool_calls) 时，把每个子任务派发成独立子图
    return [Send("subagent", {"task": t}) for t in state["pending_tasks"]]

graph.add_conditional_edges("agent", dispatch, ["subagent"])
graph.add_node("subagent", subgraph)          # 独立 messages + 受限 tools
graph.add_edge("subagent", "agent")           # 结果合并回父状态后再让 agent 汇总
```

子代理的要点：独立上下文（避免彼此污染）、只给与任务相关的工具、
各自有自己的递归上限与 token 预算，返回“结论 + 依据摘要”而不是原始长文；
父 agent 只做拆解与汇总。这个模式就是我们计划中 fan-out 的下一阶段。

## 更新记录

- **原生 checkpointer**（2026-08-13）：接入 `langgraph-checkpoint-sqlite`
  与 SafeJsonPlusSerializer，每个 superstep 自动落快照；
  `GET /api/conversations/{id}/timeline[/{checkpoint_id}]` 提供时间线审计；
  自写 checkpoint.py 继续负责跨轮中断恢复，完整回滚待下一档。
- **Hooks**（2026-08-13）：PreToolUse / PostToolUse 用户脚本回调
  （配置 `GET/PUT /api/hooks`），PreToolUse 可 deny 拦截、PostToolUse 可
  回填 additional_context，SSE 新增 `hook` 事件。
