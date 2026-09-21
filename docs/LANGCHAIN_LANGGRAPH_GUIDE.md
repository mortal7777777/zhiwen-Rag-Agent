

# LangChain 与 LangGraph 详解（结合本项目代码）

> 目标：把 LangChain/LangGraph 的核心概念和你项目里的每一处用法对上，学完能回答
> "LangChain 是什么、LangGraph 是什么、两者什么关系、你的项目怎么用的、为什么这么用"。
> 代码引用全部对应 backend/app/ 当前仓库状态。
> 前置：已读 `PYTHON_BASICS_GUIDE.md`（语法够用即可）。

---

## 1. 生态总览：LangChain 是什么

LangChain 是 **LLM 应用开发框架**，解决"把模型能力变成应用"的重复劳动：

```text
LangChain 生态分层
├── langchain-core     最底层抽象：Document / Messages / Retriever / Tools / Runnable
├── langchain-openai   模型封装：ChatOpenAI（OpenAI 兼容接口，DeepSeek 也用它）
├── langchain-text-splitters   文本切分器（RecursiveCharacterTextSplitter）
└── langgraph          状态图编排（Agent 工作流引擎，LangChain 官方出的独立框架）
```

**本项目用到的（全部）**：

| 包 | 项目里用在哪 | 文件 |
|---|---|---|
| langchain-core | `Document`（文档/检索结果载体）、`SystemMessage/HumanMessage/AIMessage/ToolMessage`（对话消息）、`BaseRetriever`（检索器基类）、`StructuredTool`（工具封装） | loader.py / llm.py / retriever.py / todos.py |
| langchain-openai | `ChatOpenAI`（DeepSeek 对话 + 查询改写两个实例） | llm.py |
| langchain-text-splitters | `RecursiveCharacterTextSplitter`（中文 child 切分） | splitter.py |
| langgraph | `StateGraph` 七节点编排、`Send` 并行子代理、原生 checkpointer | langgraph_agent.py / native_checkpoint.py |

> 面试第一句："我的项目基于 LangChain 生态——core 提供消息与检索抽象，openai 包封装模型，LangGraph 做编排。**我没有用 LCEL（线性管道）**，因为 Agent 是循环结构，LCEL 写循环很别扭，这是选 LangGraph 的核心原因。"

---

## 2. LangChain 核心抽象 × 项目对照

### 2.1 Document：一切文本的统一载体

**概念**：LangChain 用 `Document(page_content, metadata)` 统一"一段文本 + 它的来源信息"。文档加载、切分、检索、上下文，全程都传这个对象。

**项目用法**：
- 解析出的每个文本块都是 Document：`loader.py` 把 PDF/EPUB/docx 解析结果统一成 `Document(page_content=正文, metadata={"source": 文件名, "page": 页码, "relative_path": ...})`
- 切分后 parent/child 都是 Document：child 的 metadata 里带 `parent_id`（内容 md5）和 `parent_content`（父块全文）
- 检索结果也是 Document：`store.py` 的 `_hit_to_doc` 把 OpenSearch 命中转回 Document，分数/来源进 metadata

**为什么重要**：全链路统一载体后，上游（加载）不关心下游（检索/溯源）怎么用；metadata 是"附加的上下文"——溯源靠它（source/page）、父子聚合靠它（parent_id）、来源分流靠它（source）。

### 2.2 Messages：对话消息的四种类型

**概念**：聊天模型的输入/输出是消息列表，LangChain 用类型区分角色：
- `SystemMessage`：系统提示（设定行为）
- `HumanMessage`：用户输入
- `AIMessage`：模型输出（含 `tool_calls` 时表示"它要调工具"）
- `ToolMessage`：工具执行结果（必须带 `tool_call_id` 对应模型的那次调用）

**项目用法（llm.py 的 `_build_messages` + agent 主循环）**：
```python
messages = [SystemMessage(content=system_prompt_final)]   # 1. 系统提示
for item in history:                                       # 2. 历史消息
    if item.role == "user": messages.append(HumanMessage(...))
    else: messages.append(AIMessage(...))
messages.append(HumanMessage(content=question))            # 3. 当前问题
# 工具轮：模型返回带 tool_calls 的 AIMessage → 工具结果用 ToolMessage(tool_call_id=...) 回填
```

**关键细节（面试加分）**：
- 工具轮 AIMessage 必须带 `additional_kwargs["reasoning_content"]`（DeepSeek 思考模式要求原样回传，否则 400）——这就是消息类型设计的边界案例：不是所有供应商都兼容标准协议
- 消息链是**缓存前缀**的载体：顺序/内容逐字节稳定决定 DeepSeek 前缀缓存命中（见缓存优化文档）

### 2.3 ChatOpenAI：模型封装

**概念**：`ChatOpenAI` 是 langchain-openai 对"聊天补全接口"的封装，OpenAI 兼容接口（DeepSeek/SiliconFlow 等）都能用。

**项目用法（llm.py:77-92）——两个实例，温度策略不同**：
```python
self._llm = ChatOpenAI(api_key=..., base_url=..., model=...,
                       temperature=0.3,          # 回答：低温度保严谨
                       extra_body=thinking_extra_body(cfg))   # DeepSeek 思考模式参数
self._rewrite_llm = ChatOpenAI(..., temperature=0.0,          # 改写：必须确定性
                       extra_body={"thinking": {"type": "disabled"}})  # 关思考省成本
```
- `invoke(messages)`：同步调用（查询改写、HyDE、规划）
- `astream(messages)`：异步流式（SSE 逐 token，见 PYTHON_ASYNC_GUIDE）
- `bind_tools(tools)`：**给模型绑工具**——工具 schema 随请求发出，模型可返回 tool_calls（见 2.4）
- 用量收集：`callbacks=[get_usage_collector()]` 每次调用记录 token/cache（见 2.7）

### 2.4 bind_tools + tool_calls：工具调用的机制

**概念**：让模型能"调用函数"的三步：
1. `llm.bind_tools([tool1, tool2, ...])` 把工具定义（名字/schema/描述）作为元数据发给模型
2. 模型输出 `AIMessage(tool_calls=[{name, args, id}])` ——注意：模型"假装调用了"，还没真执行
3. 应用侧解析 tool_calls → 执行真实函数 → 结果包成 `ToolMessage(tool_call_id=id)` 回填 → 再给模型看

**项目用法（agent 节点，langgraph_agent.py:1286-1296）**：
```python
# 强制收尾也保持 bind_tools：工具数组是 DeepSeek 缓存前缀的一部分
chat = service.chat.bind_tools(tools)
stream = chat.stream(messages, ...)   # 流式：模型可能边"想"边输出
# 流结束后合并判定：有 tool_calls → 工具轮（缓冲文本丢弃，过渡思考不泄漏）
#                    无 tool_calls → 纯回答轮（缓冲文本作为正式回答发出）
```

**关键机制（工具轮判定）**：
- DeepSeek 等推理模型在工具轮会先输出"过渡思考"（如"我需要调用工具…"），和 tool_calls 同轮出现——流式时整体缓冲，合并后统一判定，工具轮就把缓冲文本丢弃（不给用户看思考过程）
- XML 兜底：模型偶尔输出 `<tool_calls><invoke name="bash">` 而非标准 JSON——合并文本解析兜底（:1372-1380）

### 2.5 Tools：把 Python 函数变成模型可调用的工具

**概念**：工具 = 函数 + 声明（名字、参数 schema、描述）。LangChain 两种声明方式：
- `@tool` 装饰器（自动从函数签名生成 schema）
- `StructuredTool.from_function(func, name, description, args_schema)`

**项目用法（两处都用了）**：
```python
# tools.py：@tool 声明知识库检索工具（内置 CRAG 兜底）
@tool
def knowledge_base_search(query: str) -> dict:
    """在用户的个人知识库中检索相关信息。…（何时用、返回什么）"""
    ...

# todos.py：StructuredTool 声明任务清单工具（5 个操作、数字序号匹配等细节在 func 里）
return StructuredTool.from_function(func=_invoke, name="todo_update",
    description="维护当前会话的任务清单（TodoWrite 式）…", args_schema=None)
```

**工具设计五原则**（详见面经 Q3）：名字动词开头表意准；description 讲"何时用何时不用"；参数 schema 精简；返回结构化（summary 给模型 + detail 存轨迹）；失败返回 error 字段而非抛异常（模型能看见并换方式）。

### 2.6 BaseRetriever：检索器标准接口

**概念**：`BaseRetriever` 定义"给查询返回文档列表"的标准接口（`invoke(query)`），让 RAG 检索器能插到任何 LangChain 流程里。

**项目用法（retriever.py:103-118）——把完整检索管道包装成标准 Retriever**：
```python
class KnowledgeBaseRetriever(BaseRetriever):
    retrieve_fn: Callable[[str], list[Document]]
    def _get_relevant_documents(self, query, *, run_manager=None):
        return self.retrieve_fn(query)   # 实际指向 RAGService.retrieve（全管道）

# tools.py 里：retriever = KnowledgeBaseRetriever(retrieve_fn=rag_service.retrieve)
# 这样 Agent 调 knowledge_base_search 工具 → invoke → 查询扩展+混合检索+精排全走一遍
```
> 设计点：用 `retrieve_fn` 注入避免与 RAGService 循环依赖（retriever.py 不该 import service）。

### 2.7 Callbacks：横切观察（用量/追踪）

**概念**：LangChain 的 callback 机制允许在 LLM 调用的开始/结束/流式块等时机插入钩子，用于监控、追踪、用量统计。

**项目用法（tracing.py + llm.py:97）**：
```python
# 每次调用注入"用量收集器"，线程级聚合 token/cache
config={"callbacks": [get_usage_collector()]}
# agent 节点流式调用拿不到 on_llm_end 的 usage，手动补记（langgraph_agent.py:1346）
# → agent_runs.token_usage：llm_calls/prompt_tokens/cache_hit/calls 逐调用遥测
```
> 面试点："可观测性不是后补的，是每轮模型调用通过 callback 采集的"——这是工程化意识的体现。

### 2.8 Structured Output：让模型输出结构化数据

**概念**：`with_structured_output(schema)` 让模型按 Pydantic schema 输出（底层是工具调用式约束），比"提示词 + 正则解析"稳定得多。

**项目用法（context.py 规划）**：
```python
out = self._structured(PlanSteps, prompt)   # PlanSteps 是 Pydantic schema（steps: list[str]）
# 成功 → 直接拿 steps 数组；失败 → 回退"正则从 JSON 正文抽取"（旧路径）
```
> 面试点：结构化输出失败要回退（供应商不支持/输出不合规），降级链是工程常态。

### 2.9 我们没有用的：LCEL（Runnable 管道）

LCEL（`prompt | llm | parser` 链式语法）适合**固定线性管道**（翻译、摘要、单轮 RAG 问答）。
项目旧版 `/api/chat` 的 RAG 问答本质是线性流程（检索 → 生成），但 **Agent 是"模型 → 工具 → 再回到模型"的循环**——LCEL 没有循环表达力。这就是 LangGraph 的登场点。

---

## 3. LangGraph：状态图编排

### 3.1 核心概念四件套

| 概念 | 是什么 | 项目对应 |
|---|---|---|
| `StateGraph` | 图容器，定义状态 schema | `StateGraph(AgentState)`（langgraph_agent.py:2889） |
| 节点（node） | 处理函数：读 state、干活、返回**部分状态更新** | 7 个节点：prepare/dispatch/subagent/merge/agent/tools/finalize |
| 边（edge） | 节点间转移；条件边 = 按路由函数返回值决定走向 | `add_edge` / `add_conditional_edges` |
| 状态（state） | 跨节点共享的 TypedDict | `AgentState`（字段见 §4） |

**核心执行模型**：`invoke(state)` 从 START 开始，每执行一个节点（或一批可并行的节点）是一个 **superstep**；节点返回值**合并进 state**（不是就地修改——LangGraph 的坑：节点里直接改 state 字段不生效，必须放进返回值 dict）。checkpointer 按 superstep 存快照。

### 3.2 项目图定义（背下来，面试画图用）

```python
def build_agent_graph(checkpointer=None):
    graph = StateGraph(AgentState)
    graph.add_node("prepare",  _timed_node("prepare",  _prepare_node))    # 组装上下文/工具
    graph.add_node("dispatch", _timed_node("dispatch", _dispatch_node))   # fan-out 占位
    graph.add_node("subagent", _timed_node("subagent", _subagent_node))   # 并行子代理分支
    graph.add_node("merge",    _timed_node("merge",    _merge_node))      # 合并子代理结论
    graph.add_node("agent",    _timed_node("agent",    _agent_node))      # 模型生成/决策
    graph.add_node("tools",    _timed_node("tools",    _tools_node))      # 执行工具
    graph.add_node("finalize", _timed_node("finalize", _finalize_node))   # 收尾持久化

    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", _route_after_prepare,
                                {"dispatch": "dispatch", "agent": "agent"})
    graph.add_conditional_edges("dispatch", _dispatch_tasks, ["subagent"])
    graph.add_edge("subagent", "merge")
    graph.add_edge("merge", "agent")
    graph.add_conditional_edges("agent", _route_after_agent,
                                {"tools": "tools", "agent": "agent", "finalize": "finalize"})
    graph.add_conditional_edges("tools", _route_after_tools,
                                {"agent": "agent", "finalize": "finalize"})
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)
```

**三个条件路由函数**（图的"大脑"）：

```python
def _route_after_prepare(state):   # 有可拆子任务 → dispatch；否则直接 agent
    if _build_subagent_tasks(state): return "dispatch"
    return "agent"

def _route_after_agent(state):     # 模型要调工具 → tools；计划未完成 → 推回 agent；否则收尾
    if state.get("pending_tool_calls"): return "tools"
    if state.get("force_continue"):     return "agent"   # 计划硬约束
    return "finalize"

def _route_after_tools(state):     # 工具执行完永远回 agent（ReAct 循环）
    return "agent"
```

### 3.3 循环是怎么实现的

Agent 的"多轮工具循环" = `agent → tools → agent → tools → …`：
1. `agent` 节点跑完，返回值里有 `pending_tool_calls`（模型要调的）
2. 条件边 `_route_after_agent` 看到 tool_calls → 路由到 `tools`
3. `tools` 节点执行完（结果回填消息链），条件边 `_route_after_tools` **无条件回 `agent`**
4. agent 再让模型基于工具结果决策……直到模型不调工具（或预算用尽 forced_final）→ `finalize`

**循环的终止保障（防死循环三道闸）**：
- `tool_calls_used` 预算：普通 12 / task_mode 30，超限强制收尾
- `failure_count`：连续失败 3 次强制收尾（失败不烧预算但计数）
- `recursion_limit=40`：图总步数兜底（任何意外都到不了无限）

### 3.4 Send：并行子代理

**概念**：`Send(task, payload)` 让图在**一个 superstep 内派发多个分支**（每个分支独立跑指定节点），是 LangGraph 的原生并行原语。

**项目用法（dispatch → subagent → merge）**：
```python
# _dispatch_tasks：把计划里的工具型步骤拆成多个 Send
def _dispatch_tasks(state):
    tasks = _build_subagent_tasks(state)      # [{step, tool_hint}, ...]
    return [Send("subagent", {**state, "sub_task": t}) for t in tasks]
# → 多个 subagent 分支并行执行（独立 messages + 受限工具）
# → merge 节点收集结论摘要、来源重编号、同步任务清单 → 回 agent
```
> 面试点：Send 是"主图的一个 superstep 内并行"——不是另外起线程/图，LangGraph 引擎统一调度。

### 3.5 Checkpointer：状态持久化

两层（职责不同，面试讲清）：

| | 原生 checkpointer（native_checkpoint.py） | 自写 checkpoint（checkpoint.py） |
|---|---|---|
| 粒度 | 每个 **superstep** 自动存 | 每轮工具执行后手动存 |
| 存什么 | 图执行状态 | messages/plan/todos/预算计数（业务级完整态） |
| 用途 | 时间线审计、回滚到任意中间步骤 | 任务中断后"回复继续"接着做 |
| 存储 | langgraph-checkpoint-sqlite | SQLite checkpoints 表（pending 标志） |

```python
graph.compile(checkpointer=checkpointer)   # 原生：compile 时注入
# 恢复：新请求 prepare 开头 get_store(settings).load(conv_id)
#       有 pending 快照 → 重建消息链 + 沿用原计划（plan_steps 从快照取，不重新规划）
```

### 3.6 invoke 的配置参数

```python
self.graph.invoke(state, config={
    "recursion_limit": self.recursion_limit,     # 40：图步数上限
    "configurable": {"thread_id": f"conv:{conversation_id}"},  # checkpoint 分线程
})
```

---

## 4. AgentState：状态里有什么（面试问"state 保存了哪些字段"直接背这张表）

```python
class AgentState(TypedDict):
    messages: list                # 消息链（核心：历史/系统提示/工具结果都在里面）
    question: str                 # 用户问题
    tools: list                   # 本轮工具列表（按开关组装）
    # ---- 预算与终止 ----
    tool_calls_used: int          # 已用工具调用数（成功才累计）
    failure_count: int            # 失败次数（3 次强制收尾）
    forced_final: bool            # 强制收尾标志
    last_call_warned: bool        # "只剩最后一次"提醒过没有
    # ---- 计划与任务 ----
    plan_steps: list              # 计划步骤（字符串数组）
    plan_map: list                # 每步建议工具（hint）
    plan_done_count: int          # 已完成步骤数
    plan_push_count: int          # 硬约束推回次数（≤3）
    todos: list                   # 任务清单（同步 MySQL）
    # ---- 结果与可观测 ----
    sources: list                 # 来源引用（[n] → 文档片段）
    tool_trace: list              # 工具轨迹（name/args/summary/detail/耗时）
    runtime: dict                 # 运行期对象：bus 事件总线/status/timings/token_usage/final_text
    # ---- 开关与信号 ----
    use_web_search: bool          # 联网开关（决定工具列表）
    use_knowledge_base: bool
    stop_event: object            # 停止信号（客户端断开/Ctrl+C）
    # ---- 子代理 ----
    dispatch_done: bool           # 子任务是否已派发
    subagent_results: list        # 各分支结论摘要
    sub_task: dict                # 当前子代理的任务 {step, tool_hint}
    project_dir: str              # 会话工作目录
```

**设计观察（面试加分）**：state 分四组——执行数据（messages）、控制数据（预算/终止）、业务数据（计划/todos）、运行时数据（runtime）——把"该持久化的"和"仅运行期有效的"分开，checkpoint 只存前几组，runtime 每次请求重建。

---

## 5. 一次问答的完整生命周期（跟着 state 走一遍）

```text
用户提问 "帮我对比《实践论》和《矛盾论》"
  │
  ▼
[1] prepare：组装 state
    - 加载会话历史/滚动摘要/记忆 → messages
    - context.plan(question) → ["检索《实践论》", "检索《矛盾论》", "综合对比"]（LLM 辅助调用）
    - plan_map → [knowledge_base, knowledge_base, 无]（每步建议工具）
    - 播种 todos（MySQL）→ state["todos"]
    - 按开关组装 tools（knowledge_base_search/web_search/...）
    - 路由 _route_after_prepare：有多个工具型步骤 → dispatch
  │
  ▼
[2] dispatch → Send ×2 → subagent ×2（同一 superstep 并行）
    - 子代理 A：独立 messages + knowledge_base 工具，检索《实践论》相关
    - 子代理 B：独立 messages + knowledge_base 工具，检索《矛盾论》相关
    - 各自内部跑小循环（预算 agent_subagent_max_rounds），返回结论摘要
  │
  ▼
[3] merge：合并 A/B 结论摘要 → sources 重编号 → 同步 todos 勾选 → 回 agent
  │
  ▼
[4] agent ↔ tools 循环（ReAct）
    - agent：bind_tools 流式生成 → 模型要补充检索 → tools 执行 → 回 agent
    - agent：模型觉得信息够了 → 无 tool_calls → _route_after_agent 检查：
        todos 未完成步骤？没有 → finalize
  │
  ▼
[5] finalize：来源去重 → 补完成纯推理步骤 → 消息落库 → 运行记录 + trace
    - 后台：记忆提取、标题生成
  │
  ▼
最终回答（带 [n] 来源编号）→ SSE done 事件
```

---

## 6. 面试问答（高频）

### Q1. LangChain 和 LangGraph 的关系？
> LangGraph 是 LangChain 生态里的状态图编排框架。LangChain 提供抽象（消息、检索器、模型封装、工具），LangGraph 提供**有状态、可循环、可持久化**的图执行引擎。Agent 的"模型-工具循环"是天然循环结构，LCEL 线性管道写不了循环，所以编排层用 LangGraph。

### Q2. 为什么不用裸 LCEL / 手写 while 循环？
> 两个层面：LCEL 无循环表达力；手写 while 循环虽然能跑（子代理就是这么写的），但主流程需要 checkpoint、条件路由、并行派发、超限兜底这些"框架能力"——手写循环这些全要自己造，且无法获得 LangGraph 的 superstep 快照和时间线。**取舍：主循环用图（复杂、要审计），子代理内部用手写循环（简单任务不值得上嵌套图）**。

### Q3. 你的项目里 LangGraph 具体解决了什么？（三个"没有它就很难"的点）
1. **循环 + 条件路由**：agent↔tools 的多轮循环和"计划硬约束推回 agent"的终止控制；
2. **Send 并行子代理**：多步骤任务的并行执行，每个分支独立上下文；
3. **原生 checkpointer**：superstep 级快照支撑时间线回滚——手写循环没有这个。

### Q4. LangGraph 的坑？
1. **节点里直接改 state 不生效**——必须把改动放进节点返回值 dict（项目多处踩过，如 tool_calls_used/forced_final 都靠返回值传）；
2. recursion_limit 默认值小（默认 25），工具轮多的任务会撞——设 40 并做友好兜底；
3. 状态更新是"合并"语义：TypedDict 里嵌套 dict 的原地修改不会触发持久化，要整体替换或显式返回。

### Q5. bind_tools 和缓存有什么关系？（你项目独有的深度点）
> 工具数组作为请求元数据参与 DeepSeek 前缀缓存键——工具定义变化 = 请求前缀变化 = 整链缓存失效。所以：强制收尾也保持 bind_tools（:1286）、工具列表每请求静态组装（联网开关在 prepare 定死，不在运行中增删）、工具描述/参数要瘦身（34 工具 ≈ 5.2k token/请求）。

---

## 7. 自测清单

- [ ] 能说出 Document/Messages/ChatOpenAI/BaseRetriever/StructuredTool 在项目里的具体文件
- [ ] 能画出七节点图 + 三个条件路由的走向
- [ ] 能讲清"循环怎么实现、三道终止闸是什么"
- [ ] 能说出 Send 并行子代理的完整链路（dispatch→subagent→merge）
- [ ] 能背出 AgentState 的主要字段并分四组说明
- [ ] 能回答 LangChain vs LangGraph 关系、为什么不用 LCEL、LangGraph 三个坑
- [ ] 能解释 bind_tools 与缓存前缀的关系
