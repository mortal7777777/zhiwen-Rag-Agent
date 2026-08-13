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

下一步（真正的子代理并行）：
- 用 LangGraph `Send` 做 fan-out/fan-in：把计划里的每个“工具型步骤”派给
  一个子图（各自独立上下文），完成后再由一个汇总节点合成；
- 每种子任务只暴露相关工具（KB 子代理只给 KB 工具，代码子代理只给文件/命令），
  上下文更小、更不容易跑偏；
- 汇总节点做去重与冲突说明。

### 场景 C：编程任务（写脚本 + 跑通验证）

瓶颈：现在是“模型写 → 用户批准 → 沙箱跑 → 模型看输出”，验证闭环依赖模型自觉。

下一步：
- 增加 verify 节点：写/改文件后自动跑约定命令（如 `python -m py_compile`、
  `pytest`）并把结果回填，失败自动再改一轮（有限次数）；
- 沙箱已可用：设置中把“命令沙箱”切到 docker（`command_sandbox=docker`），
  命令在 python:3.11-slim 容器里执行，`--network=none` 隔离外网。

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
