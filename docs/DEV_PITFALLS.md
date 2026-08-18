# 开发踩坑记录与缓存策略对比

> 目的：记录本项目在 MCP 工具、Agent 编排、前端交互上踩过的坑，
> 避免后续开发重复犯错；并与 Claude Code 的缓存策略对比给出优化建议。
> 持续追加，每个条目含「现象 → 根因 → 解法/预防」。

---

## 一、MCP 工具参数：schema 声明 ≠ 服务端实际校验

### 1.1 显式 null vs 缺省（最痛，先后踩了 browser_snapshot / browser_tabs / 等）

**现象**：调用 `browser_tabs({action: "list"})` 报
`Invalid input: expected number, received null → at index / url`。

**根因**：Playwright MCP 服务端用 zod 校验，字段声明 `.optional()` 只接受
**缺省（undefined）**，拒绝**显式 null**。而客户端 `_schema_to_model` 生成的
pydantic 模型把所有可选字段填成 `None`，`model_dump()` 后序列化为 JSON null
发给服务端 → 被拒。JSON 里"缺省"与"null"是两回事。

**解法**：`_mcp_default_args`（`backend/app/mcp_manager.py`）在发送前统一
剔除 null（`{k: v for k, v in merged.items() if v is not None}`）；
同时在 `_tools_node` 对 `mcp_` 工具 invoke 前也剔除 LLM 显式传的 null
（否则 pydantic 校验阶段就拒绝，报 `Input should be a valid ...`）。

**预防**：所有 MCP 客户端发送参数前都做一次 null 剔除，不要假设
"可选字段可以传 null"。

### 1.2 required + default 的字段不能按严格必填处理

**现象**：`mcp_browser_console_messages` / `mcp_browser_network_requests`
空参被客户端"参数缺失"拦截，但服务端空参其实可用默认值正常工作
（level 默认 "info"、static 默认 false）。

**根因**：服务端 schema 把带 `default` 的字段声明进 `required`
（zod `.default()` 的产物），`_schema_to_model` 原先无脑按必填解析 →
`_tool_has_required_args` 误判拦截。

**解法**：`_schema_to_model` 尊重 schema 的 `default` 属性——
`required` 且无 `default` 才按必填，否则按"可选+默认值"。

### 1.3 schema 声明可选、服务端运行时却要求必填（三选一/二选一）

**现象**：`browser_wait_for` 空参报
`Either time, text or textGone must be provided`；`browser_find` 空参报
`Provide either "text" or "regex"`。

**根因**：这是服务端 handler 的**运行时检查**（不是 zod null 检查），
null 剔除救不了空 dict。

**解法**：`_MCP_TOOL_DEFAULT_ARGS` 按工具名补默认值
（wait_for 补 `{"time": 1}`，已实测服务端接受；text+time 组合也 OK）。
`browser_find` 的 `{"text": ""}` 实测**被服务端拒绝**（空串不过 truthiness
检查）——不要补无意义默认，让空参得到服务端干净错误、模型补参重试即可。

### 1.4 通用结论

- Playwright MCP（第三方）schema 与其实际校验长期不一致，客户端必须多层兜底：
  发送前剔 null → 工具名默认值 → 干净报错让模型自愈。
- **验证前提**：用户报错/猜测要先 live 实测（连上服务器 `list_tools` + 空参/带参
  调用），本例用户声称"服务端把 index/url 声明为必填"，实测 schema 只有
  `action` 必填、枚举是 `list/new/close/select`（不是 switch/create）。
  枚举名写错会导致模型一直调错工具。

---

## 二、pydantic 解析的隐性行为

- `Field(default=None)` 配非 Optional 注解（如 `float`）：
  **缺省时 dump 为 None，但显式传 None 会被校验拒绝**
  （`Input should be a valid number`）。所以"剔除 null"要发生在
  **两个阶段**：invoke 前（防 pydantic 拒收）和发送前（防服务端拒收）。
- 判断"是否显式传参"必须看**值是否为 None**，而不是 key 是否存在——
  pydantic 解析后可选字段的 key 永远存在（值为 None）。

---

## 三、XML 工具调用标记：变体与流式切分

### 3.1 全角符号 / |DSML| 前缀变体

**现象**：assistant 消息里出现 `<｜DSML｜tool_calls>`（全角竖线 U+FF5C），
且最终回答原样带出，任务以垃圾文本收尾（"泄露然后停止"）。

**根因**：模型的工具调用标签可能是全角变体（`｜＜＞` = U+FF5C/U+FF1C/U+FF1E）、
`<|DSML|...>` 前缀、`<antml:invoke>`、`<user|tool_calls>` 等。检测标记、
解析、清洗的正则只认 ASCII `<tool_calls`/`<invoke` → 全部漏判 → 工具轮判定
False → 正文带标签直接进 `final_text` → 存库 → **模型看到历史里的变体标签
还会模仿，泄漏自我延续**。

**解法**（`langgraph_agent.py`）：
- `_normalize_tool_markers()`：全角符号转 ASCII、去 `|DSML|`/`|user|` 前缀，
  在 `_agent_node` 的 merged_text 上先归一化，再做标记检测/解析/存储判定。
- `_parse_xml_tool_calls` / `_strip_xml_tool_tags` 入口也归一化（双保险）。
- 解析失败时"有界重试"（提示模型改用标准格式，最多 2 次），避免静默停止。
- 泄漏进历史的消息要清理（打破模仿循环）。

### 3.2 流式分片会切碎标签

逐 chunk 检测 XML 标记会因标签跨分片漏判 → 工具调用信息泄漏进最终回答。
**判定必须在合并后的完整文本上做**（缓冲全部流式内容，流结束统一判定）。

### 3.3 清洗要整块删除

只删标签不删内部参数值，命令/query 等内容会残留泄漏。
先整块删 `<tool_calls>…</tool_calls>` / `<invoke>…</invoke>` 等（含参数），
再清残留孤立标签；`finalize` 收尾再清一次（覆盖跨片残留）。

---

## 四、前端：选择器静默失败与 CSS 自引用宽度

### 4.1 querySelector 匹配不到 + optional chaining = 无声失效

**现象**：审批选项卡出现后按 Enter/数字键完全无效。

**根因**：`data-perm-id` 在**外层包装 div** 上，`focusPermissionCard` 的
选择器写成 `.permission-card[data-perm-id="..."]`（要求同一元素同时有
class 和属性）——永不匹配 → `card?.focus()` 静默 no-op → 焦点从未进卡片
→ 键盘 handler 的 `inCard` 检查永远不通过。

**预防**：选择器与模板结构对照检查；`?.` 会掩盖 bug，必要时
匹配不到就打日志/回退，不要静默。

### 4.2 百分比 max-width 的自引用收缩

**现象**：用户气泡"两个字显示成两行"、长文本换行宽度只有整行 ~62%。

**根因**：DOM 是 `.msg-body > .user-msg-group（flex column, align-items:
flex-end）> .user-bubble`。`.user-msg-group` 收缩到内容宽度，气泡
`max-width: 70%` 以**自身包裹层宽度**为参照 → 循环依赖被浏览器解成
"气泡 ≤ 70% 自身宽" → 短文本被压窄拆行。

**解法**：`.user-msg-group { width: 100% }` 撑满整行（气泡的 70% 才有
稳定参照）；`.msg-body` 也要 `flex: 1 1 auto; min-width: 0`。

**教训**：**CSS 复现测试页必须用真实 DOM 结构**——第一版测试页少了
`.user-msg-group` 包裹层，没复现出 bug，误判"已修复"。

---

## 五、工程与环境

- **行尾符**：Windows 编辑器（PyCharm 等）保存会写 CRLF，仓库基线是 LF →
  整个文件显示为改动（601 行 vs 实际 62 行）。提交前检查
  `git diff --stat`，异常大时用 `git diff --ignore-space-at-eol` 对比，
  用 `python -c` 转回 LF 再提交。
- **pytest 临时目录权限**：`pytest-of-user` 目录偶发
  `PermissionError`（Windows 环境问题），与代码无关，重跑/清目录即可。
- **vite dev server 只监听 IPv6**（`[::1]:5173`）：`curl localhost` 会
  HTTP 000，用 `curl -g "http://[::1]:5173/..."` 或从浏览器验证。

---

## 六、缓存策略：与 Claude Code 对比与优化建议

### 6.1 本系统现状

- 消息排序已按"缓存友好"设计（`langgraph_agent.py` _prepare_node）：
  静态核心（模板+工具规则）→ 项目记忆 → 历史消息 → 动态块
  （todos/记忆/时间戳）→ 问题。注释目标"长任务累计命中率 80%+"。
- 工具结果进历史前瘦身（`_slim_tool_result`，>8000 字符保留首尾）。
- 实测最近 6 个 run：命中率 43%~71%（均值 ~55%）。

### 6.2 Claude Code 为什么命中率高

1. **静态前缀 + 动态后缀**：system prompt（工具定义、CLAUDE.md、skills、
   hooks 文档）字节级稳定放最前；每轮只**追加**用户消息和工具结果，
   从不改写历史。活跃会话每 5 分钟内有交互 → 缓存基本不会过期。
2. **工具结果截断**：超大输出截断/省略化，控制前缀增长。
3. **上下文自动管理**：接近上限时自动 compact（摘要早期内容），
   前缀仍稳定 → 命中率可长期保持 90%+。
4. **缓存是显式标记的**（Anthropic prompt caching），TTL 短（5min），
   命中即 0.1 倍价；DeepSeek 是**自动前缀缓存**（无需标记，TTL 约 1h）。

### 6.3 本系统差距与优化建议

| 项 | Claude Code | 本系统现状 | 建议 |
|---|---|---|---|
| 前缀稳定性 | 静态核心+CLAUDE.md 最前 | 已对齐（核心/记忆/历史/动态块） | 维持；AGENTS.md 等记忆文件变更会断缓存（低频可接受） |
| 工具定义稳定性 | 会话内不变 | `bind_tools` 每轮重建，MCP 工具列表来自服务器 | **检查**：工具顺序/描述/动态工具（用户自写工具、技能注入）会话内是否字节稳定；工具 schema 序列化顺序靠 dict 有序性 |
| 恢复路径 | 恢复即续前缀 | 已修：time_context 曾插在 messages[1]，跨分钟恢复断缓存 | 保持动态内容一律放消息末尾 |
| 上下文自动压缩 | 自动 compact | 仅手动 /compact | 建议：历史超阈值自动压缩（先摘要旧轮再继续），对齐 auto-compact |
| 命中率可视化 | 内部有统计 | runs 表已存 cache_hit/cache_miss | 建议：Web 运行详情展示命中率，便于验证优化效果 |
| 子代理 | 独立上下文 | 子代理独立消息链，无法共享父缓存 | 结构性限制，可接受；子代理摘要已瘦身 |
| 每轮首调命中 | 接近 100%（前缀未变） | 第一轮 miss 占比仍高（均值 55% 的主要来源） | 用 6.3 的工具定义稳定性检查 + 恢复路径修复后复测 |

**已落地**（实测验证）：
- 工具定义 hash 监测：`_tools_prefix_hash` 记录到 run 的
  `token_usage.tools_hash`，与上一 run 对比告警（实测同会话 4 轮 hash
  完全一致，机制有效）。
- 实测 4 轮连续会话命中率：6% → 6% → 92% → 30%。冷启动首轮 6%（正常）；
  同会话连续轮次可达 92%；个别轮次掉到 6-30%——此时 tools_hash 一致、
  消息历史干净、动态块都在末尾，**客户端前缀并未变化**，波动来自
  DeepSeek 自动缓存的服务端行为（64-token 粒度、缓存条目淘汰），
  非本系统可再优化的点。
- Web 运行详情与用量卡已展示缓存命中率（RunsView.vue）；自动压缩已有
  （`compact_conversation` 软窗口：条数超限或 token 超预算时摘要旧轮）。
- 修复统计 bug：流式调用 `llm_calls` 重复计数（on_llm_end 空 usage 也
  计数 + 手动补记）→ tracing.py 空 usage 跳过。

**剩余可选**（成本从低到高）：
1. 换用显式缓存管理的 provider（Anthropic cache_control），命中率可预测。
2. 命中率波动持续影响成本时，考虑减少每轮动态注入内容（已最小化）。

---

## 附：本次会话修复清单（对应提交 a2c11f6）

- MCP：null 通用剔除、schema default 尊重、browser_wait_for 默认 time=1、
  browser_tabs 占位 index/url（枚举注释修正 list/new/close/select）。
- Agent：XML 全角/|DSML| 变体归一化、mcp_ 工具 invoke 前剔 null、
  恢复路径 time_context 移末尾。
- Web：审批卡片聚焦选择器修复、用户气泡宽度修复。
- 测试：XML 变体、MCP 默认值、schema default 语义（91 个全过，
  26 个环境权限报错与代码无关）。
