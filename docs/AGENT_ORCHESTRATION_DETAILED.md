# Agent 编排详解(LangGraph 状态图全节点解剖)

> 依据 `backend/app/agent/langgraph_agent.py`(3210 行,核心)与
> `context.py` / `agent.py` / `permissions.py` / `checkpoint.py` /
> `native_checkpoint.py` / `mcp_manager.py` / `todos.py` / `skills.py` /
> `prompts.py` / `hooks.py` / `tracing.py` 源码逐行核对。
> 适合:面试前深挖实现、改代码前查行为、调试疑难问题。

---

## 1. 总览:一次问答怎么跑起来的

```text
前端 POST /api/agent/stream (SSE)
  │
  ├─ 创建 Queue + stop_event + run_id(取消注册表 _ACTIVE_RUNS)
  ├─ 启动 daemon worker 线程 → graph.invoke(state, {recursion_limit:30,
  │    configurable:{thread_id:"conv:{id}"}})
  │     └─ 节点事件经 EventBus 写入本 run 的 Queue
  └─ 主协程从 Queue 取事件 → SSE 推给前端(15s 心跳保活)
```

关键设计:**每个流式请求完全隔离**——独立队列、独立 worker 线程、
独立 `AgentState`、线程级 token 收集器(`threading.local`)。多会话并行互不干扰。

### AgentState(状态字典,图节点间传递)

```python
service / bus / db / runtime / stop_event      # 基础设施
question / images / use_web_search / use_knowledge_base
conversation_id / template_id / system_prompt / tool_mode
plan_only / resume_plan / project_dir / command_sandbox
messages / tools / pending_tool_calls          # 主状态
sources / tool_trace / counter([0] 引用编号器)  # 结果与轨迹
tool_calls_used / failure_count / forced_final / last_call_warned
plan_steps / plan_map / kb_documents / memory_hits / memory_summary
vision_descriptions / summary_text / todos
plan_done_count / plan_push_count / force_continue / xml_retry_count
dispatch_done / task_mode / subagent_results / sub_task
title_holder / title_thread / persist_start / early_created
```

---

## 2. 图结构与条件路由(全表)

```text
                    ┌──────────┐
                    │  START   │
                    └────┬─────┘
                         ▼
                    ┌──────────┐
                    │ prepare  │  会话/历史/checkpoint/规划/记忆/视觉/工具组装
                    └────┬─────┘
              ┌──────────┴───────────┐
              │ _route_after_prepare │
              ▼                      ▼
      ┌──────────────┐       ┌──────────────┐
      │   dispatch   │       │    agent     │  LLM 流式生成
      │ (占位节点)    │       └──────┬───────┘
      └──────┬───────┘              │ _route_after_agent
             │ Send 扇出             ▼ tools | agent | finalize
      ┌──────▼───────┐       ┌──────────────┐
      │ subagent ×N  │       │    tools     │  执行/审批/验证/进度
      └──────┬───────┘       └──────┬───────┘
             ▼                      │ _route_after_tools
      ┌──────────────┐              ▼ agent | finalize
      │    merge     │
      └──────┬───────┘
             ▼
      ┌──────────────┐
      │    agent     │ (主循环)
      └──────┬───────┘
             ▼
      ┌──────────────┐
      │  finalize    │  收尾/落库/运行记录/done
      └──────┬───────┘
             ▼
             END
```

| 路由 | 条件 | 去向 |
|---|---|---|
| `_route_after_prepare` | stop_event 已置位 | agent(直接收尾) |
| | `agent_subagents_enabled` 关闭 或 dispatch 已完成 或 无工具型计划步骤 | agent |
| | 否则 | dispatch |
| `_route_after_agent` | stop_event 已置位 | finalize |
| | 有 pending_tool_calls | tools |
| | force_continue(计划硬约束/XML 重试) | agent |
| | 否则 | finalize |
| `_route_after_tools` | stop_event 已置位 | finalize |
| | 否则 | agent |

> **关键点**:7 个节点**始终注册**在图中(`build_agent_graph`,2887 行),开关只在
> 路由函数运行期生效——关闭子代理不是重建图,而是 `_route_after_prepare`
> 永远不选 dispatch 分支。

---

## 3. prepare 节点(每轮开销最重的节点)

按执行顺序逐段拆解(`_prepare_node`,227 行):

### 3.1 会话与历史
- 会话不存在 → `create_conversation`(新会话标记);已存在且换模板 → 更新会话模板;
- `compact_conversation` 软窗口压缩(见 §13)→ `trim_history_for_budget` token 裁剪
  → `_rows_to_history` 从 DB 重建历史消息(支持 system/tool/工具轮 assistant 行);
- 任何异常 → **降级为无记忆模式**(日志告警,不阻断问答)。

### 3.2 自定义 checkpoint 恢复
- 条件:checkpoint_enabled 且 **不是**"任务未完成的继续轮"(task_mode 且 todos 未完成时
  本轮就是续做,不再重复恢复);
- 命中恢复 → 原样复用快照消息链,只追加"恢复提示 + 项目记忆 + 轨迹摘要 + 问题",
  并恢复 sources/tool_trace/tool_calls_used/forced_final 等全部进度字段(567-606 行);
- 恢复链以快照内既有 system_prompt_core 开头,**不再重复插入**(会前缀错位)。

### 3.3 标题后置
- 仅新会话:后台线程用 `title_chat`(temperature=0,思考关闭)生成 ≤20 字标题,
  完成写库;首 token 不被阻塞,`finalize` 时若线程已结束则推送 `title` 事件。

### 3.4 规划(Plan-and-Execute 轻量版)
- `tools_enabled = 知识库开关 or 联网开关 or advanced_tools_enabled`;
- 计划模式确认后(`resume_plan`)沿用已确认计划;**checkpoint 恢复沿用原计划**;
- `context.plan()`:`_looks_complex` 判定(≥30 字或含"总结/分析/对比…"关键词)
  → `with_structured_output(PlanSteps)` 优先,失败回退正则 JSON 解析;≤5 步;
- `_plan_hint` 给每步打工具提示(knowledge_base_search / web_search / file_tool-bash);
- `task_mode`(项目级任务)判定:关键词("完成/实现/整个项目…")或计划 ≥4 步或
  长问题且计划需要工具 → 工具预算 6→24、失败上限 3→6。

### 3.5 任务清单(TodoWrite)播种
- `refresh_todos_for_plan`:无新计划且清单全完成 → 清空;有新计划且与现 plan 项一致
  → 沿用进度;不一致 → 用新计划替换 plan 项、保留 manual 项(去重步骤文本);
- 进度 `plan_done_count/plan_push_count` 恢复时从快照取。

### 3.6 知识库文档清单
- `rag.list_documents()[:30]` 注入动态块,让模型知道"库里有什么",避免直接联网。

### 3.7 技能目录
- `skills_enabled` + `load_prefs` 启用集合 → `build_skill_catalog`(名字+一句话,≤40 条)
  → 只进**动态块**(避免每轮变化破坏缓存前缀)。

### 3.8 系统提示词三段式(为 prompt caching 服务,核心设计)

```text
① system_prompt_core(static_only=True)
   模板 + 工具规则 + 计划模式说明       ← 跨轮字节级稳定,消息第 1 条
② static_dynamic_text(dynamic_only=True)
   技能目录 + 知识库文档清单            ← 会话内字节级稳定,放 history 前
③ todos_prompt_text(dynamic_only=True)
   任务清单                           ← 每轮变化,放 history 后(D 块)
```

`compose_system_prompt`(prompts.py:238)支持 static/dynamic 拆分;静态核心含
模板 + 知识库/联网/编码工具纪律 + 计划模式只读声明;动态部分含任务清单硬约束规则、
技能目录 + 技能安全护栏(不可信资料声明)、文档清单。

### 3.9 长期记忆召回(GPU 锁内)
- `rag.acquire_gpu()` → `retrieve_memories`(关键词 n-gram 粗筛 100 候选 +
  BGE 嵌入精排,阈值 0.35,Top3 + 最近补位,token 预算 600)→ `release_gpu()`;
- `get_memory_summary` 常驻画像摘要。

### 3.10 视觉预识图
- 条件:有图片 + vision_auto_describe + 主模型无视觉 + SenseNova configured;
- `describe_images` 一次性把最多 6 张图生成描述 → `vision` 事件。

### 3.11 分层消息组装(纯追加链)

```text
[system_prompt_core]                  ← 缓存命中区(静态)
[static_dynamic_text]                 ← 缓存命中区(会话内稳定)
[history_messages(DB 重建)]            ← 缓存命中区(只追加)
persist_start = len(messages)         ← ★ 落库起点
[项目记忆 AGENTS.md] [todos] [轨迹摘要] [会话摘要] [画像摘要] [记忆命中] [视觉描述] [时间] [问题]
```
- D 块(每轮变化)统一收在问题之后/之前,只影响链尾;`persist_start` 标记
  落库起点,保证"下一轮请求 = 上一轮请求 + [D 块,问题]"(纯追加,字节级前缀稳定)。

### 3.12 工具组装(优先级从低到高)
1. `build_tools`:knowledge_base_search(+add_document)/web_search/vision/skill_lookup,
   按开关与 `counter`(本次对话全局引用编号器);
2. `service.mcp_tools(db)`:启用中的 MCP 服务器工具(`mcp_` 前缀);
3. `service.extra_tools`:受控执行文件/命令工具集(advanced_tools_enabled 总开关);
4. `load_user_tools()`:用户自写工具(动态目录,每次请求重建时生效);
5. `make_todo_tool`:todo_update(独立短会话读写,线程安全);
- **工具定义哈希**:`_tools_prefix_hash` 记入 run,与上一 run 对比,变化即告警
  (工具数组是缓存前缀的一部分,会话内变化断缓存)。

---

## 4. 子代理并行(dispatch → subagent×N → merge)

### 4.1 dispatch(876 行)
占位节点,真正的扇出在**条件边函数** `_dispatch_tasks`:`Send("subagent", {...})`
每个子任务一个独立分支(并行 superstep)。

### 4.2 子任务构建(`_build_subagent_tasks`)
- 遍历 `plan_map`,取带 `tool_hint` 的步骤,按 `hint|step[:40]` 去重,**最多 4 个**。

### 4.3 subagent 节点(986 行)
- 独立上下文:`[SUBAGENT_SYSTEM_PROMPT, HumanMessage(原始问题 + 子任务)]`,
  独立 `counter=[0]`;
- 受限工具集(按 hint 选):KB 步骤只给 knowledge_base_search(禁联网兜底);
  web 步骤只给 web_search;文件步骤给 list/read/grep/write/edit/delete/bash;
- 最多 `agent_subagent_max_rounds`(默认 2)轮,每轮 bind 工具调用;
  两轮后仍无结论 → 强制补一次"结论摘要"调用;
- **敏感操作走与主 Agent 一致的 HITL**(`_run_sensitive_subagent_tool`):白名单
  命中或本会话已记住直接执行;否则 submit → `permission_request` 事件 →
  wait → 拒绝时返回"用户拒绝了该操作"错误,模型说明影响并询替代方案;
- 结论摘要清洗 XML 工具调用标记(防泄漏进主 Agent 上下文);
- 用量走**辅助收集器**(`get_aux_usage_collector`),与主循环分开统计。

### 4.4 merge 节点(1121 行)
- `_renumber_subagent_sources`:各分支局部编号重排为全局编号(防 [n] 冲突);
- 每个分支:tool_trace 追加进主轨迹;`tool_start/tool_result` 事件(名称
  `subagent`);摘要拼成 `### 子任务名\n摘要` 块,以 SystemMessage 注入主上下文;
- `sync_todos_from_plan` 按完成数推进任务清单(只勾工具型步骤),发 `plan_progress`;
- `dispatch_done = True`(避免恢复后重复派发)。

---

## 5. agent 节点(LLM 流式生成,1225 行)

1. **最后一次提示**:剩余调用数 ==1 且未提示过 → 注入"只剩最后一次机会,
   优先补充检索,之后必须作答";
2. **思考摘要前置**:工具轮数 >0 且未发过 → 等后台预生成的摘要(最多 1s,
   超时不阻塞)→ `reasoning` 事件;
3. **LLM 并发锁**:`acquire_llm()` 包围流式调用(生成阶段不占 GPU 锁);
4. **强制收尾保持 bind_tools**:`forced_final` 下仍绑定工具(工具数组是缓存前缀),
   仅当 `forced_tool_rounds >= 2` 才解除绑定兜底终止;
5. **流式缓冲 + 整轮判定**:chunk 全部缓冲;**判定必须基于合并后的完整文本**
   (XML 标签跨分片会漏判)。合并文本判定:
   - 有 tool_calls 或 XML 工具调用标记 → **工具轮**:缓冲文本(过渡思考)整体丢弃,
     不展示;AIMessage 只存 `stored_content=""`;
   - 否则 → **回答轮**:逐片清洗 XML 标记后经 `token` 事件发出;
6. **DeepSeek 思考模式回传**:带 tool_calls 的 assistant 消息必须
   **无条件回传 reasoning_content(含空串)**,否则 API 400;
7. **XML 兜底解析**:`_parse_xml_tool_calls` 支持 `<invoke>`/`<tool_use>`/
   `<function>`/`<antml:invoke>` + `<parameter>`/`<arguments>`/裸 JSON;
   解析失败且未强制收尾 → 注入提示"改用标准工具调用",重试最多 2 次;
8. **计划硬约束**:无 tool_calls 且未强制收尾且 push 次数 <3 → 检查剩余计划步骤
   是否仍需要工具,是 → `force_continue=True`,注入"【计划硬约束】…继续调用工具,
   不要提前输出最终回答;某步无需执行先用 todo_update remove/complete";
9. **工具轮判定后的 token 聚合**:`usage_metadata` 手动补记入线程收集器 +
   `calls` 逐调用遥测(in/out/read/t_ms)。

---

## 6. tools 节点(工具执行 + 回填,1949 行)

### 6.1 HITL 审批门(顺序)
1. 清理过期审批请求(`cleanup`,1 小时);
2. **计划模式拦截**:plan_only 且敏感工具 → 直接 blocked,不弹审批;
3. **ask 模式**逐个工具调用判定:
   - `is_sensitive_tool`(SENSITIVE_TOOLS:write_file/edit_file/delete_file/bash/
     command_tool/add_document + 用户工具声明 + file_tool 敏感操作);
   - bash/command_tool 命中**命令白名单**或**会话已记住** → 不需确认;
   - 否则 `permission_manager.submit`(唯一 UUID)→ `permission_request` 事件 →
     `wait` 阻塞本 run 线程(超时 `permission_timeout`,0=无限;stop_event 中断);
   - 批准:记住标记写回;拒绝:结果注入"操作未执行(用户拒绝):原因,请说明影响
     并询问替代方案";
4. **allow 模式**全部自动批准。

### 6.2 执行(线程池并行)
- `ThreadPoolExecutor(max_workers=min(3, len(tool_calls)))` 并行执行,保持返回顺序;
- **per-run 工具缓存**:纯查询工具(知识库/联网/list_dir/read_file/grep_search/
  skill_lookup)同参数重复调用命中缓存,标注"（缓存命中）";副作用类绝不缓存;
  `add_document` 成功后清空检索缓存;
- **PreToolUse hooks**:deny 拦截(结果注入"操作被 hook 拒绝");post 回填
  additional_context;
- **空参数兜底**:schema 有必填且调用为空 → 返回"参数缺失,请补充后重试"
  (不消耗预算);MCP 全可选工具放行,由 `_mcp_default_args` 补默认值;
- **MCP null 剔除**:发送前去掉显式 null(StructuredTool pydantic 解析后
  可选字段变 None,Playwright 服务端 zod 拒 null);

### 6.3 结果回填
- ToolMessage 内容 **slim 瘦身**(content 超 2500 字符保留首尾,列表超 60 项截断);
- 轨迹 entry:summary 清洗 XML 标记、detail(完整输出截断版)供展开;
- sources 抽取(知识库/联网 → 引用来源卡);
- **PostToolUse hooks** + 附加上下文注入;
- **失败不烧预算**:error 或非零退出码 → `failure_count+1`,注入"这是一次可重试的
  失败,不消耗你的工具调用预算…"引导(先 list/read/grep 确认现状 → 修正参数 →
  不可达则说明并给替代方案);成功数才累计 `tool_calls_used`;

### 6.4 写后自动验证(verify)
- write_file/edit_file 成功后 `_run_verify`:显式 `verify_command` 优先,
  否则按扩展名自动检测(.py→py_compile/.js→node --check/.json→json.tool/
  .yaml→yaml 解析);docker 沙箱内路径映射 /workspace;
- 失败 → 注入错误与修复引导,`VERIFY_MAX_RETRIES` 控制复验次数,超限要求如实说明;

### 6.5 计划进度同步
- 成功后重新读 DB 任务清单;成功的非 todo_update 调用推进计划步骤
  (`sync_todos_from_plan` 只勾工具型步骤);`plan_progress` 事件 + 进度文本注入;

### 6.6 强制收尾(两个触发)
- `tool_calls_used >= 上限`(task_mode 24/普通 6)→ forced_final=True,注入
  "预算用尽:进度汇报 + 回复继续"或"已达上限,直接作答";
- `failure_count >= 上限` → 同上,注明失败原因与用户需要做什么;

### 6.7 checkpoint 保存
- 每轮工具执行后 `get_store(settings).save(conv_id, {...})`:messages(手动
  序列化,保留 reasoning_content)/sources/tool_trace/plan/todos/进度字段全量落
  SQLite(`opensearch_meta/checkpoints.db`,按 conversation_id 主键)。

---

## 7. finalize 节点(2562 行)

1. **收尾清理**:完整文本再清一次 XML 标记 + strip;
2. **任务清单收尾**:纯推理/总结类步骤自动补完成;未完成的工具型步骤保留未勾选
   (审计留痕);补发最终 `plan_progress`;
3. **来源去重**(按 index,上限 8);
4. **联网附录**:回答完全没标 [n] 且有 web 来源 → 自动补"**参考来源**"链接;
5. **纯追加链落库**(缓存保真的关键):
   - 从 `persist_start` 起按链顺序持久化:用户问题行插在链中对应位置;
     SystemMessage→system 行;ToolMessage→tool 行(带 tool_call_id/name 配对);
     工具轮 AIMessage→assistant 行,内容为 `{"__tool_calls__":[...],
     "__reasoning__":"..."}` 标记 JSON(空 reasoning 也存空串);
   - 下一轮 `_rows_to_history` 重建 = 上一轮实际发送逐字节一致;
6. **运行记录 + trace**:`create_agent_run`(question/plan/tool_trace/latency/
   status/token_usage{汇总,last_call,calls,timings,tools_hash}) + JSONL trace;
7. **轨迹压缩**:工具调用 ≥3 次 → `compress_trajectory` 生成过程摘要,下次注入;
8. **清 checkpoint**(正常/停止收尾);
9. **标题补发** + `done` 事件(ok/stopped/sources/tool_trace);
10. **后台记忆提取**(不阻塞流):显式"记住"或回答 ≥400 字 → extract_facts →
    add_facts(嵌入去重)→ maybe_consolidate(24h 间隔/10 条阈值)→
    export_project_memory(AGENTS.md)。

---

## 8. HITL 人工审批专题

### 8.1 PermissionManager(permissions.py)
```python
_requests: dict[str, PendingRequest]   # 全局注册表,键 = 唯一 UUID
_session_allowed: dict[int, set[str]]  # 会话级"不再询问"白名单
```
- `submit` → 生成 `perm_{uuid}` 请求(带 conversation_id)→ 返回;
- `wait` → 轮询 `req._event`(0.5s 间隔):stop_event 置位 → 拒绝"用户停止了回答";
  超时 → 拒绝"等待确认超时,已自动取消";
- `resolve(request_id, approve, reason, remember_*)` → 按 ID 找到请求 → `_mark`
  置 decision 并 set 事件 → 唤醒**发出该请求的等待线程**;
- 多会话并发审批:**天然隔离**(每个请求独立 UUID + 独立线程事件),
  这是"审批卡不会返错会话"的第一层保证。

### 8.2 审批生命周期(前端视角)
```text
tools 节点 submit → SSE permission_request{id,name,arguments,summary}
  → 前端渲染审批卡(路径/内容预览/edit diff/命令)
  → 用户批准/拒绝(附备注)/记住 → POST /api/agent/permission/{id}/resolve
  → _mark → wait 返回 → permission_resolved 事件 → 卡片折叠为状态条
```

### 8.3 三层豁免
| 层 | 机制 |
|---|---|
| 命令白名单 | `command_allowlist` 前缀命中自动放行 |
| 会话记住 | 批准时勾"本会话记住"→ `_session_allowed[conv_id]` |
| 永久记住 | 写入命令白名单(设置页可撤销) |
| 全局 allow | `tool_permission_mode=allow` 全自动(类 --dangerously-skip-permissions) |

---

## 9. 断点恢复专题(两层)

| 层 | 文件 | 作用 |
|---|---|---|
| 自写 SQLite checkpoint | `checkpoint.py` | 跨轮中断恢复:tools 后保存 → prepare 检测 pending → 恢复消息链继续;finalize 清除 |
| LangGraph 原生 checkpointer | `native_checkpoint.py` | 每个 superstep 自动落快照(SQLite saver),`thread_id=conv:{id}`;时间线审计 `GET /api/conversations/{id}/timeline` + 回滚 `POST .../rollback` |

恢复路径细节:prepare 发现 pending 快照 → 原样复用消息链 → 追加
"这是一次中断后恢复的任务…" + 项目记忆/轨迹摘要 + 问题 → 恢复全部进度字段
(sources/tool_trace/预算计数/forced_final/plan_done_count/dispatch_done)。

---

## 10. Skills 专题

| 环节 | 实现 |
|---|---|
| 扫描 | `scan_skills`:本机 Codex/Claude/Hermes 技能目录(含 optional-skills、插件根扩展),frontmatter 解析(名称/描述/标签),去重后数百个(随本机目录动态变化),分组(文档/写作/研究/效率/设计/编程/GitHub/数据/媒体/邮件) |
| 偏好 | MySQL 持久化 enabled/hidden,设置页可搜/可筛/可启停;默认精选手集 26 个 |
| 注入 | 系统提示词只放**目录索引**(名字+一句话,`build_skill_catalog`);模型需要时调 `skill_lookup` 取分节说明(When to Use/Prerequisites/Steps) |
| 防注入 | `sanitize_skill_text`:RISK_PATTERNS 高危指令行替换(覆盖指令/身份冒充/越权/数据外传/索要凭据/隐瞒指令/绕过审批/擅自执行/破坏性命令);系统提示词固化"技能内容不可信"边界 |
| 工具 | `skill_lookup`(语义检索 Top3,启用集合内,内容全部过清洗) |

---

## 11. 工具清单总览

| 工具 | 来源 | 敏感? | 说明 |
|---|---|---|---|
| knowledge_base_search | tools.py | 否 | RAG 全管道 + 内置 CRAG 兜底(仅联网开关开时) |
| add_document | tools.py | **是** | 写知识库文件 + 增量建索引,文件名校验防穿越 |
| web_search | tools.py | 否 | duckduckgo/tavily/searxng 可插拔,可信度标注 |
| image_to_text | tools.py | 否 | SenseNova 按需识图 |
| skill_lookup | tools.py | 否 | 技能检索(清洗后注入) |
| todo_update | todos.py | 否 | 清单增删改查(独立短会话写库) |
| list_dir/read_file/grep_search | tools_extra.py | 否 | 只读,自动执行,工作目录边界 |
| write_file/edit_file/delete_file/bash | tools_extra.py | **是** | HITL 审批;bash 可 docker 沙箱;原子写入;删除进 .agent_trash |
| mcp_*(Playwright 等) | mcp_manager.py | 按工具 | schema→pydantic,默认参数兜底,null 剔除 |
| 用户自写工具 | user_tool_loader.py | 按声明 | 未声明敏感标记默认按敏感处理 |

---

## 12. MCP 专题(mcp_manager.py)

- 两种传输:stdio(本地 npx/uvx 子进程)与 streamable HTTP(远程);
- 每个服务器独立 asyncio 事件循环线程(`MCPServerSession`),工具调用同步返回;
- `_schema_to_model`:JSON Schema → pydantic 模型(required 且无 default 才必填,
  尊重 default);
- Playwright schema 缺陷兜底 `_MCP_TOOL_DEFAULT_ARGS`(browser_snapshot 补
  target/depth/boxes、browser_tabs 补 index/url、browser_wait_for 补 time);
- **通用 null 剔除**:StructuredTool 经 pydantic 后可选字段为 None,发送前剔除
  (zod .optional() 拒 null);
- 连接失败自动降级(跳过该服务器);`MCPManager` 为单例(跨会话共享连接)。

---

## 13. 上下文工程与缓存(性能核心)

### 13.1 滚动摘要(粘滞窗口)
- 窗口起点 = 摘要边界(summary_up_to_id),未压缩时逐字节不变(前缀缓存跨轮命中);
- 触发:窗口行数 > 1.5×上限(默认 400→600) 且/或 token > 1.25×预算才压缩一次,
  压到上限条数;旧逻辑"超 60 就压到 60"每轮改写历史开头、缓存每轮失效(2026-09-10 修复);
- 预算按模板差异化:general/knowledge 64k / coding·writing 96k / translate 64k
  (HISTORY_BUDGET_* 可覆盖;兜底 HISTORY_MAX_TOKENS);
- 压缩与压缩后裁剪统一 budget(`history_budget_for`,此前裁剪写死 32k);
- 摘要增量合并(已有摘要先裁剪再拼接);`trim_history_for_budget` 最小回收门控:
  回收 <15% 预算放弃裁剪。

### 13.2 长期记忆
- 提取:显式"记住"信号(MEMORY_INTENT_PATTERNS)强制;否则回答 ≥400 字才后台提取;
- 召回:n-gram 粗筛(100 候选)→ BGE 语义精排(阈值 0.35,Top3 + 最近补位,
  token 预算 600);
- 整合:24h 间隔 + 活跃 ≥10 条触发;合并重复/新覆盖旧/归档过时 + 生成画像摘要;
- 去重:文本相等 + 向量相似度 >0.95。

### 13.3 前缀缓存(纯追加链)
- 静态核心 → 技能/文档清单 → 历史 → D 块 → 问题,字节级稳定;
- 落库保真:finalize 从 persist_start 按链顺序落库(含 system 行/工具轮标记),
  下一轮重建与上一轮实际发送逐字节一致;
- 工具哈希跨轮监测;forced_final 保持 bind_tools;工具结果进历史前 slim(2500);
- 遥测:逐调用 `calls`(in/out/read)+ cache_hit/miss 聚合。

---

## 14. 并发、锁与会话隔离

| 资源 | 机制 | 位置 |
|---|---|---|
| GPU(embedding/rerank/记忆召回) | `BoundedSemaphore(MAX_CONCURRENCY=4)` | rag/service.py:53 |
| LLM API 调用 | `BoundedSemaphore(LLM_MAX_CONCURRENCY=8)` | rag/service.py:55 |
| 审批请求 | 唯一 UUID + 每请求事件 | permissions.py |
| 用量收集 | `threading.local` 线程隔离 | tracing.py:23 |
| 取消 | run_id → stop_event 注册表 | api/agent.py:28 |
| 消息/记忆/清单/checkpoint | 全部按 conversation_id 键控 | repository/checkpoint/todos |
| MCP 连接 | 单例共享(浏览器工具并行需注意) | mcp_manager.py |

---

## 15. 可观测性

- `agent_runs` 表:question/plan/tool_trace/latency/status/token_usage
  (汇总 + last_call + 逐调用 calls + 分阶段 timings + tools_hash);
- `_timed_node` 包装器给 7 个节点计时(prepare/dispatch/subagent/merge/agent/
  tools/finalize + TTFT),finalize 落库;
- JSONL trace:`opensearch_meta/traces/YYYY-MM-DD.jsonl`(线程级 token 聚合);
- `/api/metrics`:请求量/错误率/检索/生成/扩展平均耗时;
- 预留 Langfuse 接入点。

---

## 16. 面试一句话

"编排用 LangGraph 显式状态图:prepare 组装分层上下文(静态核心+历史+D 块,
纯追加链保持前缀缓存字节稳定)→ 条件路由决定走 Send 子代理并行段还是主循环
→ agent⇄tools 循环(工具轮缓冲判定、XML 兜底解析、reasoning 回传)→ finalize
纯追加落库;HITL 审批按唯一 ID 唤醒对应线程、三层豁免(白名单/会话记住/永久);
断点两层(自写 SQLite 恢复 + 原生 checkpointer 时间线回滚);开关(子代理/技能/
沙箱)全部是运行时条件路由,图结构不变。"
