# 知问 ZhiWen vs Claude Code：Harness 设计对比分析

> 本文基于 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)
> （Claude Code 泄露源码的教学复刻，Python 实现，s01~s17 共 17 章，约 12.7k 行）与
> ZhiWen 项目（FastAPI + LangGraph 7 节点图 + DeepSeek 自动前缀缓存 + MySQL + 本地 BGE，v1.0）的
> 实际代码逐项对比。对比日期 2026-09-07。

---

## 0. 先说结论

1. **两套系统在同一张"harness 设计蓝图"上**：工具 + 上下文 + 记忆 + 权限 + 观察（s15 的 `PROMPT_SECTIONS` 与 ZhiWen 的组装链几乎逐项对应）。ZhiWen 已经覆盖 Claude harness 的**核心 80%**。
2. **最大的架构差异是缓存策略的适配**：Claude 用显式 `cache_control` 断点（system/tools 打点、大而稳的前缀）；ZhiWen 为 DeepSeek 自动前缀缓存做了**纯追加链 + 链尾收敛**（D 块），实测轮内 92-99%、run 级 76-92%（v4-flash 模型，2026-09-07 A/B 实测），是同一句"稳定性优先"的不同方言。
3. **缺的部分多是外围件**：后台任务/定时任务/多智能体/目标环（goal loop）/workflow 运行时——它们对个人知识库场景并非刚需。
4. **ZhiWen 有教学仓库都没有的反超点**：逐调用缓存遥测（`calls[{in,read,out,t_ms}]`）、工具级耗时/阶段耗时（`timings`）、审计 trace 等可观测性远超该教学实现。

---

## 1. 总纲：harness 五要素对照

| 要素 | Claude Code（harness 复刻） | ZhiWen | 备注 |
|---|---|---|---|
| 工具 | 26 个内置 + MCP（s15）；查表分发 | 约 34 个（文件/终端/KB/联网/记忆/Todo/MCP Playwright 24 个并入） | 两侧都支持 MCP 动态并入 |
| 知识 | Skills 目录 + 按需 load_skill（s07） | SKILL.md 扫描 → 目录常驻 + skill_lookup 工具（`prepare.py`） | **同构** |
| 观察 | git diff / 工具输出 / transcript | 工具结果 slim 2500 字符 + trace JSONL | 见 §7 |
| 动作 | 单循环 while（s01） | LangGraph 状态图（prepare→dispatch→subagent→merge→agent→tools→finalize） | 编排哲学差异，见 §2 |
| 权限 | DENY_LIST→规则→ask_user 三道闸（s03） | 敏感工具表 + 命令白名单 + 会话记忆 + HITL 弹窗（`permissions.py`） | 见 §6 |

---

## 2. 编排哲学：单循环 vs 状态图

**Claude harness**（s01）：`while True` 循环是唯一内核——模型产出 `tool_use` 块 → 逐块执行 → `tool_result` 以"新 user 消息"回喂 → 无工具调用即退出。全部 17 章机制（hooks/todo/compact/记忆/后台/cron/teams）都挂在**循环的钩子点**上，循环本体不变（s04 把钩子抽象成 4 事件，s15 里每轮调用顺序固定）。

**ZhiWen**：`langgraph_agent.py` 显式状态图 + 条件路由，7 个节点各司其职，`prepare`（组装/规划/记忆/清单）→ `agent`（LLM 决策）→ `tools`（执行/权限/摘要预生成）→ 上节条件判断（是否还有工具调用/是否强制收尾/计划是否完成）→ `finalize`（落库/trace/轨迹压缩）。

**差异与取舍**：

| | 单循环 + 钩子 | 状态图 |
|---|---|---|
| 可读性 | 机制多但路径唯一，读代码 = 读循环 | 节点职责清晰，条件路由一眼看懂 |
| 扩展性 | 新机制=新 hook（Pre/PostToolUse 短路语义） | 新机制=新节点或加入条件路由 |
| 控制流 | 模型全自动，harness 几乎不"拦" | 可由代码强制回推（计划硬约束、强制收尾、调用上限） |
| 调试 | 单步跟踪简单，但机制交织 | graph 状态快照式推理更清晰 |
| 反模式 | 教学仓库明确反对"流程编排器"（README 原文） | LangGraph 本身支持 Send 分叉并行，无"Rube Goldberg"嫌疑 |

ZhiWen 的图并不是教学仓库批评的"过程编排器"——它仍是模型主导、节点只做环境操作（检索/工具/落库），且 `Send()` 分支子代理正是图上并行（等价 s15 的并行语义）。**结论：ZhiWen 的图 = Claude harness 循环 + 显式阶段职责，实现方式不同、设计等价。**

---

## 3. 上下文组装：每轮重拼 vs 纯追加链

### 3.1 Claude harness（s15 assemble_system_prompt）

每轮**重拼** system prompt，固定顺序：

```
identity → tools → tasks → teams → workspace → memory → compaction → 当前时间
→ Skills catalog → Memory catalog → Relevant memory records → Connected MCP servers
```

（s09：记忆注入 system prompt；memory catalog 给全量索引、relevant records 只给召回的正文，`RECALL_CHAR_LIMIT=20000`）

### 3.2 ZhiWen（prepare.py:421-467）

组装一次（每 run），分层固定：

```
[静态核心 system_prompt_core]      ← 跨会话字节稳定
[技能目录+文档清单 static_dynamic]   ← 会话内稳定
[历史（DB 重建，纯追加）]            ← finalize 保证字节保真
--- D 块（链尾变化区）---
[项目记忆 AGENTS.md][todos][轨迹摘要][会话摘要][画像摘要][记忆召回][图片][时间戳]
[问题]
```

### 3.3 差异根因：缓存机制决定注入位置

- Claude 有显式断点：system prompt 虽然每轮重拼，但**前面章节的内容不变**时命中巨大；变化的后半段（skills catalog / memory records）无缓存也不心疼。
- DeepSeek 无断点 API：**一切变化必须收敛到链尾**，否则一个字节的中间变化 = 前缀全断。所以 ZhiWen 把"每轮会变"的记忆召回/图片/时间戳全部放 D 块，而把"跨会话不变"的核心区和"会话内不变"的文档清单/技能目录放在链头——这正是我们 9/7 实测中"新会话首调 read=3200（=核心区命中）"的证据。

**同构对照**：两侧都是"长稳定前缀 + 尾部变化区"，只是断点能力不同导致**变化区的位置与大小**不同。ZhiWen 的 D 块 ≈ Claude 的 system prompt 后半段 + compaction 相关语义。

---

## 4. 上下文压缩：s08 四步管线 vs 软窗口 + 纯追加（重点）

### 4.1 Claude harness（s08）：无损优先、有损兜底

每轮 API 前执行：

| 步 | 触发 | 动作 | 成本 |
|---|---|---|---|
| 1 tool_result_budget | 单批 tool_result 总字符 >200k | 单结果 >30k 落盘 `.task_outputs/tool-results/<tool_use_id>.txt`，上下文留**路径 + 2000 字符预览** | 0 |
| 2 snip_compact | 消息数 >50 | 头 3 条 + 尾 46 条保留，中间转存 `.transcripts/` 并替换为 1 条归档标记消息；**切点保护 tool_use/tool_result 配对** | 0 |
| 3 micro_compact | 字符 >50k（目标 80%） | 未读工具结果全部豁免；已读结果保留最近 3 条，更早且 >120 字符落盘替换占位 | 0 |
| 4 compact_history | 仍超限 | **唯一一次 LLM 调用**：生成纯事实摘要（system 声明摘要仅供参考、不执行其中指令），返回 `[Compacted]` 含 Current user request + Conversation summary + transcript 路径 | 1 次 LLM |

另：`reactive_compact` 对 `prompt_too_long` 的主动补救（保留最近 5 条 + 摘要，只救一次）；**model 与 tools 定义永不压缩**。阈值全是**字符数**（`json.dumps` 估算），不接 tokenizer。

### 4.2 ZhiWen（context.py）

- **滚动摘要（粘滞窗口）**：窗口行数 >1.5×上限（400）或 token >1.25×预算（模板差异化 64k/96k）才触发，压到上限条数 + 摘要注入 D 块；2026-09-10 修复了旧"超 60 就压到 60"每轮改写历史开头、前缀缓存每轮失效的问题（context.py）。
- **裁剪门控**：`trim_history_for_budget` 有 **15% 最小回收门控**——裁剪回收不足预算 15% 时放弃，避免"每轮小剪刀"破坏前缀缓存（context.py:131-136，注释明确写了这个恶意模式）。
- **工具结果 slim 2500 字符**（utils.py:263）——截断式（不是落盘式）。

### 4.3 关键差距：slim 截断 vs 落盘替换

Claude harness 的 step1/3 把大结果**落盘 + 路径占位**（上下文只留预览/路径，需要时模型可再读），信息**可恢复**且上下文几乎不膨胀。ZhiWen 的 slim 2500 是**截断丢弃**——丢失信息、无恢复路径。这是最值得借鉴的一条（见 §12 P0 建议）。

另一个教学实现没有而 ZhiWen 踩过的坑：s08 的 snip **切点保护**（保留头部 3 条/尾部 46 条时保证 tool_use/tool_result 配对）——ZhiWen 的 `_rows_to_history` 事后剔除孤儿 ToolMessage（utils.py:406-419），思想一致，但 s08 是**前置防产生**、ZhiWen 是**后置修复**，若压缩切点设计先做保护更稳。

---

## 5. 缓存：差异的核心（结合 9/7 实测）

**教学仓库不含 cache_control**（全库 grep 零命中）——它裁剪接缝处不处理缓存 API。真实 Claude Code 的缓存设计要点是：

1. 大而稳定的前缀：system + 工具 + CLAUDE.md（跨会话稳定，约 30-60k token）；
2. `cache_control` 断点打在 system / tools / 对话尾（ephemeral，5 分钟 TTL），客户端显式声明"这段要缓存"；
3. 每轮增量小（几 k），长会话 20-60 轮，首调占比被稀释到 1-3% → **run 级 95%+**。

**ZhiWen 现状（2026-09-07 A/B 实测，同代码同会话）**：

| 模型 | 增量提交速度 | 逐调用命中 | run 级 |
|---|---|---|---|
| deepseek-v4-flash | 700-1400 tok/s | 20s 间隔 99%，短间隔 22-31% | Q3 轮内 89-99% |
| deepseek-v4-flash-vision-exp | 20-40 tok/s | **平台化 3.2-5.5k**，10-16s 只推进 256 token | 27-37% |

对照历史（8/18-8/27，标准模型）：轮内长间隔 95-98%、run 级最高 84-92%。

**差距的成因链**：前缀大小（ZhiWen ~10k vs Claude ~40k+）→ 首调稀释比例；断点（无）→ 只能靠字节稳定 + provider 提交速度；模型选择 → 30-50 倍吞吐差异（vision-exp 拖垮一切）。

**结论**：ZhiWen 的纯追加链是"无断点约束下的最优解"（三处实现：finalize 落库保真、`_rows_to_history` 系统行重建、时间戳放链尾），缓存差距多为 provider 与产品形态（前缀长度）所致，而非设计缺陷。

---

## 6. 权限：三道闸 vs 敏感表 + HITL + 白名单

| | Claude harness（s03） | ZhiWen（permissions.py + tools.py:100-154） |
|---|---|---|
| 结构 | DENY_LIST 硬拒绝（字符串匹配）→ 规则表（谓词 lambda）→ ask_user y/N | 敏感工具表（write/edit/delete/bash/add_document）→ 命令白名单（自动放行前缀）→ 会话记忆（本会话不再询问）→ ask/allow 模式 |
| 路径安全 | safe_path 工作区约束 | 绝对路径必须落在 workspace 内、拒绝路径穿越、可操作错误提示（tools_extra.py） |
| 依赖链 | **无**（教学实现无"edit 需先 read"） | 无显式依赖链（读类自动执行） |
| 确认交互 | 终端 y/N | SSE permission_request + 审批卡片 + 数字键 1/2/3 + `resolve` API，等待确认可超时（默认 0=无限，可配） |
| 拒绝语义 | 拒绝结果以 tool_result 文本回喂 | 拒绝原因回传 + 模型解释替代方案 |
| 白名单 | 无 | 命令白名单（前缀匹配自动放行）+ 永久记住（撤销入口） |

**差异总结**：ZhiWen 的权限系统是 Claude Code 系产品的"正派"形态（支持 allow 全自动/ask 逐条/白名单/记住），教学仓库的三道闸是简化教学版。ZhiWen 在 HITL 交互深度（审批卡片快捷键、diff 行级渲染）上明显超出教学实现。

---

## 7. 工具系统与 MCP

| | Claude harness | ZhiWen |
|---|---|---|
| 注册 | `TOOLS`（给模型 schema）+ `TOOL_HANDLERS` 字典，双表并联 | langchain `bind_tools(tools)`，Pydantic schema 自动生成；MCP 经 `_schema_to_model` 转换 |
| 工具数 | 26 内置 + MCP 动态并入 | 约 34 + MCP（Playwright 24） |
| MCP 命名 | `mcp__{server}__{tool}` 规范化 + 64 字符限制 + **冲突检测** | mcp_manager 前缀式命名 |
| MCP 安全 | **HOST_POLICY 宿主侧白名单，不信任 server readOnlyHint** | 无参调用兜底（schema 缺陷修补）+ 权限按用户工具敏感性 |
| 工具结果 | 落盘/比例控制（见 §4） | slim 2500 截断 |

值得注意：教学仓库 MCP 的"宿主侧白名单"与 ZhiWen 的"权限系统兜底"是同一个思想的两种强度——ZhiWen 可考虑为 MCP 工具默认 require 确认（当前 permission system 已 covers）。

---

## 8. 规划与 Todo

| | Claude harness（s05） | ZhiWen（todos.py） |
|---|---|---|
| 工具 | todo_write（整表替换，≤20 项，三态，in_progress 唯一） | todo_update（list/add/complete/remove/set 全套操作） |
| 持久 | 内存 | **MySQL per-conversation，跨轮恢复** |
| 提醒 | 连续 3 轮未更新追加 `<reminder>Update your todos.</reminder>` | 计划硬约束：存在未完成工具型步骤时路由**回推 agent 继续**（最多 3 次），plan_done_count 审计 |
| 联动 | 仅文本渲染 | 前端计划卡片自动勾选联动 + 工具执行后自动回填"已完成 x/y 步" |

**评价**：ZhiWen 的 todo 是本轮对比中**反超点**——Claude harness 只做"提醒"，ZhiWen 做"路由强制 + 清单自动推进 + 前后端联动"。

---

## 9. 子代理：层级委派 vs 并行分叉

- Claude harness（s06）：`task` 工具，新 messages + SUB_SYSTEM，上限 30 轮，只回最终文本；**SUB_TOOLS = BASE_TOOLS - {task}**（单层委派）；共享 WORKDIR 与相同权限/Hooks。
- ZhiWen：`Send` 图分支子代理——多步骤计划在 dispatch 阶段拆给并行子代理（只读/检索工具），merge 节点汇总来源与轨迹；subagent 有独立上下文与工具集（subagent.py）。

差异：ZhiWen 是**并行**（多个子代理同时跑，适合"多来源检索"），Claude harness 是**串行委派**（一个 task 一个）。ZhiWen 的 merge（来源/轨迹合并、任务清单同步）比"只回最终文本"更结构化。

---

## 10. 记忆系统（s09 vs 三层记忆）

| | Claude harness（s09） | ZhiWen |
|---|---|---|
| 存储 | `.memory/*.md` + MEMORY.md 索引（文件） | MySQL memories 表（分类：画像/偏好/项目/决定/教训/其他） |
| 分层 | user/project/session scope（CLAUDE.md 三层） | 会话滚动摘要（短期）+ 长期记忆（语义召回）+ 项目 AGENTS.md（文件型） |
| 选择 | **轻量 LLM 调用**从目录挑 ≤5 条（失败退化关键词） | **本地 BGE 向量召回**：n-gram 粗筛 → embed 精排 → 相关性不足用最近记忆补位（context.py:334-408） |
| 注入位置 | system prompt 两节（catalog + records） | D 块链尾（DeepSeek 适配：每轮变化一次） |
| 提取 | 回合边界提取，persistent 才跨会话 | 每轮对话自动提取（显式"记住"强制）+ 分类 + 去重 |
| 整合 | consolidate（阈值 10） | 自动整合（24h 间隔 + 10 条阈值，合并/新覆盖旧/归档/生成画像摘要） |
| 摘要 | — | 常驻用户画像摘要（≤150 字） |

**评价**：ZhiWen 的向量召回对"个人知识库助手"更合适（确定性、零 LLM 成本、无幻觉），Claude harness 的 LLM 选择器适合"工具命令上下文"。两者注入位置差异由缓存策略决定（§3.3），**ZhiWen 的选择在当前约束下正确**。

---

## 11. 外围件：ZhiWen 缺的模块与评估

| 模块 | Claude harness | ZhiWen | 必要性（个人知识库场景） |
|---|---|---|---|
| 后台任务（s11） | bash run_in_background → daemon 线程 → `<task_notification>` 注入 | 无（长命令阻塞轮，60s 超时） | ★★★ 长编译/爬虫可用，值得借鉴 |
| 定时任务（s12） | cron 队列（durable 至少一次交付） | 无 | ★☆ 演示性用途低 |
| 多智能体 teams（s13） | 用户确认 spawn、mailbox 协作 | 无 | ☆ 教学展示为主 |
| workflow 运行时（s16） | registry + agent/parallel/pipeline 原语 + journal 快照续跑 | LangGraph 图（声明式等价物） | ★★ 已有等价结构；journal 续跑对应 checkpoint |
| 目标环（s17） | /goal：模型提出停止 + 独立评估器（小模型、看证据、block 回喂、双上限） | 计划硬约束（代码规则回推） | ★★ 风格差异：规则 vs 独立 LLM 评估（ZhiWen 的规则更便宜确定，Claude 的更自愈但多一次调用） |

---

## 12. 可借鉴清单（按性价比排序）

**P0（高收益，可直接落地）：**
1. **工具结果落盘 + 路径占位**（s08 step1/3）：替代现在的 slim 2500 纯截断。大工具结果写 `.agent_trash/tool-results/<tool_use_id>.txt`，上下文留 `[完整输出已存 路径] + 2000 字符预览`，模型需要时用 read_file 恢复。收益：大结果不膨胀上下文、不丢信息、且与纯追加链兼容（路径字符串字节稳定可缓存）。
2. **压缩切点配对保护**（s08 snip）：压缩/裁剪前先检查 tool_use/tool_result 配对（头端越过结果、尾端前移），比 `_rows_to_history` 的后置孤儿剔除更干净（前置免生成）。

**P1（中收益）：**
3. **bash 后台任务**（s11）：`run_in_background` 标志 + 完成通知注入（独立事件、不复用 tool_use_id，保证一调一对）——长任务体验友好。
4. **MCP 宿主侧白名单**（s14）：MCP 工具默认走权限确认，不信任 server 的 readOnlyHint。
5. **目标环或"独立评估收尾"**（s17）：需要独立小模型评估"任务是否真正完成"时启用，当前 plan 硬约束规则已覆盖大部分场景，可作插件式扩展。

**不建议照搬：** cron/teams/LLM 记忆选择器（有更优替代）；字符级预算（ZhiWen 有 `estimate_tokens` 中文更准）。

---

## 13. 反超点（ZhiWen 优于该教学 harness）

1. **可观测性**：逐调用遥测 `calls[{in,read,out,t_ms}]`（tracing.py）、阶段耗时 `timings`（prepare/agent/tools/TTFT）、工具定义哈希 `tools_hash`、trace JSONL、`/runs/stats` 成本与命中率仪表盘——教学仓库只有 log_hook。
2. **缓存工程深度**：纯追加链 + 字节验证 + 15% 裁剪门控 + 无变化不写盘 + 跨轮工具遥测，远deepHarness教学实现。
3. **HITL 体验**：审批卡片、数字键快捷操作、diff 行级预览、会话记住/永久记住/撤销。
4. **路径安全**：workspace 门控 + 路径穿越拒绝 + 可操作错误提示 + `.agent_trash` 可恢复删除。
5. **多模态**：SenseNova 视觉接入、图片理解注入 D 块——教学仓库无。
6. **开源工程形态**：MySQL 三表 + OpenSearch + 子代理 + Docker lite/full 分档。

---

## 14. 结论

- **设计同源**：ZhiWen 与 Claude Code harness 覆盖同一份蓝图（工具/上下文/记忆/权限/观察），实现语言与编排形态不同（Python/状态图 vs Python 复刻/单循环）。
- **缓存是核心分水岭**：显式断点 vs 自动前缀，决定了注入位置、首调稀释、run 级上限。ZhiWen 的纯追加链方案在 DeepSeek 下已是实践验证的最优适配（轮内 92-99%）。
- **差距集中在"外围长尾"**与 **slim-截断-不落盘** 这两处；其余大量机制（todo 强制/子代理/技能按需/MCP）ZhiWen 已实现且部分反超。
- **改进优先级**：P0 工具结果落盘替换（s08 step1）+ 压缩切点保护；P1 bash 后台任务与 MCP 权限默认化——已在 §12 给出。

> 文档引用编号：学习仓库按 s01~s17 章节；ZhiWen 引用 backend/app 内文件与行号。
