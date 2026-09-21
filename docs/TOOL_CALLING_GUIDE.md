# Tool Calling(工具调用)详解:概念、组成与应用

> 依据 `agent/langgraph_agent.py`(agent/tools 节点、XML 兜底、工具轮判定)、
> `agent/tools.py`(工具工厂)、`tools_extra.py`(受控执行工具)、
> `mcp_manager.py`(MCP 桥接)逐行核对。本项目的 tool calling 是
> **OpenAI 风格 function calling + DeepSeek XML 变体兜底** 的实战形态。

---

## 1. 概念:Tool Calling 是什么

### 1.1 定义

**Tool Calling(函数调用)= 让 LLM 在生成文本之外,输出"结构化工具调用意图"**:
模型不执行工具,只声明"我要调用 `web_search`,参数是 {query: "…"}"。
由应用层执行工具,把结果作为消息回传给模型,模型继续推理。

```text
用户: 2026 年 AI 开源模型有哪些?
模型: (thinking) 需要联网 → 输出 tool_calls:
      [{name: "web_search", arguments: {"query": "2026 开源大模型"}}]
应用: 执行 web_search → 返回结果 ToolMessage
模型: 基于结果生成最终回答
```

### 1.2 为什么需要(核心价值)

1. **知识截止**:模型训练数据有时效,工具(联网/检索)补充实时信息;
2. **能力外包**:计算器、数据库、文件系统、浏览器——模型"指挥",系统"执行";
3. **幻觉抑制**:RAG 场景模型只基于检索上下文作答;
4. **可审计**:每次调用留痕(本项目 tool_trace 全记录)。

### 1.3 两种主流协议形态(本项目都处理)

| 形态 | 结构 | 使用方 |
|---|---|---|
| OpenAI 风格 | 消息带 `tool_calls: [{id, name, arguments(JSON)}]`,工具结果以 `tool_call_id` 配对 | DeepSeek 主流输出 |
| XML 风格 | 文本里写 `<tool_calls><invoke name="bash"><parameter name="command">ls</parameter></invoke></tool_calls>` | DeepSeek/sensenova 偶发兜底输出 |

---

## 2. 组成:一次工具调用的完整生命周期

### 2.1 消息协议(本项目实际落库格式)

```text
user      : 问题
assistant : 带 tool_calls 的 AIMessage
            - content = ""(工具轮过渡思考不展示)
            - tool_calls = [{id, name, args}]
            - additional_kwargs.reasoning_content(DeepSeek 思考模式必须原样回传)
tool      : ToolMessage(content=JSON 结果, tool_call_id 配对, name)
assistant : 最终回答(纯文本)
```

### 2.2 工具定义(bind_tools)

```python
@tool
def web_search(query: str) -> dict:
    """联网搜索...返回标题、链接和摘要(每条带 index 引用编号...)。"""
    ...
# 模型侧:chat.bind_tools([web_search, knowledge_base_search, ...])
```

- 工具描述 = **给模型的说明书**(何时用、返回什么、引用编号规则),
  描述质量直接决定调用准确率;
- 工具 schema 参与**缓存前缀**(tools 数组字节稳定才能命中 DeepSeek 前缀缓存
  ——本项目强制收尾也保持 bind_tools 的原因)。

### 2.3 工具注册清单(本项目全部工具)

| 类别 | 工具 | 执行层 |
|---|---|---|
| RAG | knowledge_base_search / add_document | rag/service.py 全管道 |
| 联网 | web_search | duckduckgo/tavily/searxng |
| 视觉 | image_to_text | SenseNova |
| 技能 | skill_lookup | skills.py |
| 任务 | todo_update | todos.py(独立短会话) |
| 文件 | list_dir / read_file / grep_search(只读自动) | tools_extra.py(工作目录边界) |
| 命令 | write_file / edit_file / delete_file / bash(敏感 HITL) | tools_extra.py(白名单+超时+docker 沙箱) |
| MCP | mcp_*(Playwright 等) | mcp_manager.py |
| 用户自写 | 动态目录加载 | user_tool_loader.py |

---

## 3. 项目实现拆解:agent 节点的工具轮判定

### 3.1 流式缓冲 + 整轮判定(核心设计)

```text
chat.stream(messages, bind_tools(tools))
  → 所有 chunk 缓冲
  → 合并完整文本后统一判定:
      有 tool_calls(标准) 或 含 XML 工具调用标记 → 工具轮:
        缓冲文本(过渡思考)整体丢弃,不展示给用户
      否则 → 回答轮:逐片清洗 XML 标记后 token 事件流出
```

**为什么必须整轮判定**:XML 标签(如 `<tool_calls>`)可能**跨分片**,
逐片检测会漏判 → 标签与参数泄漏进最终回答(实测修复过的 bug)。

### 3.2 XML 兜底解析(`_parse_xml_tool_calls`)

- 识别 `<invoke>` / `<tool_use>` / `<tool_call>` / `<function>` /
  `<antml:invoke>`;
- 参数三种形态:① `<parameter name="k">v</parameter>`;② `<arguments>{JSON}</arguments>`;
  ③ 裸 JSON;
- 参数值尝试 JSON 解析(数字/布尔/数组/对象),null 跳过;
- **标记归一化**:全角变体(`＜｜DSML｜tool_calls＞`)与 `|DSML|` / `|user|`
  前缀先转标准标签,否则检测/解析/清理全部漏判;
- 解析失败且未强制收尾 → 注入提示"改用标准工具调用",重试最多 2 次
  (`xml_retry_count`),避免"静默停止"。

### 3.3 展示与历史的隔离

- 工具轮 content 不写入历史(`stored_content=""`),只保留 tool_calls——
  避免过渡思考文本进上下文污染下一轮;
- 回答文本中残留的 XML 标记:`_strip_xml_tool_tags` 整块删除
  (含内部参数值,防命令/query 泄漏),分片逐片 + finalize 收尾再清一次;
- 工具结果进历史前 **slim 瘦身**(2500 字符保留首尾,列表截 60 项);
- reasoning_content **无条件回传**(空串也回传),否则 DeepSeek 思考模式 400。

### 3.4 tools 节点执行(并行 + 缓存 + 兜底)

```text
pending_tool_calls → HITL 审批门(敏感工具,详见编排文档 §6)
  → ThreadPoolExecutor(≤3 并行)执行:
      per-run 缓存(纯查询工具同参命中)
      PreToolUse hooks 拦截
      空参数兜底(schema 有必填 → 提示补参,不烧预算)
      MCP null 剔除 + 默认参数补齐
  → 结果回填 ToolMessage + 轨迹 + 来源 + 失败不烧预算 + verify + 进度同步
```

### 3.5 子代理的受限工具集

- 子代理按计划步骤 hint 只给对应工具(KB 步骤只有 knowledge_base_search,
  web 步骤只有 web_search,文件步骤给全套只读+写);
- 敏感操作走与主 Agent 一致的 HITL;独立 counter 编号,merge 时重排。

---

## 4. 应用:典型链路演示(多步任务)

```text
用户:"写一个 python 脚本打印当前时间,保存并运行验证"
  → prepare:规划 3 步,播种任务清单
  → agent:tool_calls=[write_file{path: now.py, content: ...}]
  → tools:HITL 审批(write_file 敏感)→ 批准 → 执行 → verify 自动
       (py_compile)→ 结果回填 + 进度推进
  → agent:tool_calls=[bash{command: "python now.py"}]
  → tools:白名单未命中 → HITL 审批 → 执行 → 输出回填
  → agent:无 tool_calls → 最终回答 + 轨迹汇总 → finalize 落库
```

---

## 5. 面试题精讲

### Q1. Function Calling 的原理是什么?
**答法**:模型训练时见过"函数+参数 schema",推理时输出格式被约束为
tool_calls JSON(名称+参数);应用层执行后把结果作为 tool 消息回传,
模型继续推理——本质是"把外部能力编进对话格式"。
**加分**:讲清 assistant→tool→assistant 的消息循环;讲 JSON schema 驱动的
参数校验。

### Q2. 工具描述重要吗?
**答法**:非常重要——模型靠描述决定"何时调、调哪个、填什么参数"。好描述:
何时该用(触发条件)、返回结构、与其他工具的边界、引用规则。
**加分**:项目实战:knowledge_base_search 描述明确"涉及书籍/文档时必须先调
本工具,不确定某本书是否在库中也应调用";web_search 描述声明"知识库已有
的优先 KB"——直接决定调用准确率。

### Q3. 工具调用轮怎么处理模型的"过渡思考文本"?
**答法**:工具轮模型常先输出"我将调用工具…"再输出 tool_calls。策略:
流式缓冲整轮,合并文本判定有 tool_calls/XML 标记 → 缓冲文本整体丢弃,
不展示不进历史;回答轮才发出。
**加分**:讲跨分片漏判的坑;讲"过渡思考进历史"会导致下一轮重复发送、
污染上下文、破坏缓存前缀。

### Q4. 模型输出 XML 风格工具调用怎么办?
**答法**:兜底解析:检测标记(含全角/DSML 变体)→ 解析 name/参数(parameter/
arguments/裸 JSON)→ 转标准 tool_calls 执行;解析失败注入提示重试(≤2 次);
正文残留标记在展示前整块清除。
**加分**:提归一化(全角、|DSML| 前缀)是这类问题的隐藏难点。

### Q5. 工具调用会失败,怎么设计容错?
**答法**:① 失败不烧预算(可重试错误只记 failure_count);② 注入引导
(先 list/read/grep 确认现状→修正参数→不可达给替代方案);③ 空参数不浪费
调用;④ 超时/上限强制收尾兜底;⑤ 写后自动验证闭环。
**加分**:这是项目 tools 节点的完整失败处理链,面试可一口气讲出。

### Q6. 工具结果为什么进历史前要瘦身?
**答法**:模型本轮已看过完整输出,下一轮只需 summary+关键片段做决策;
瘦身降 token 成本、缩短上下文;且**不破坏缓存前缀**(小输出原样保留)。
**加分**:提 Hermes proactive_prune 借鉴;提"瘦身只在进历史时做,
展示层保留完整 detail"。

### Q7. tool_calls 与 MCP 有什么关系?
**答法**:模型侧的 function calling 决定"调什么",MCP 是执行层的接入协议。
本项目把 MCP 工具包装成 LangChain StructuredTool 注册进 bind_tools,
模型无感;执行时走 MCP 服务器转发。
**加分**:提 schema 桥接(null 剔除/默认参数)是两者衔接的工程细节。

---

## 速记卡

- 生命周期:bind_tools → 模型输出 tool_calls → HITL/执行 → ToolMessage 回填 → 再推理;
- 工具轮判定:整轮缓冲合并文本,有 tool_calls/XML 标记即工具轮,过渡文本丢弃;
- XML 兜底:归一化(全角/DSML)→ 解析(parameter/arguments/裸 JSON)→ 重试 ≤2;
- reasoning_content 无条件回传(含空串),否则 400;
- 执行层:并行 ≤3、per-run 缓存、空参兜底、失败不烧预算、verify 闭环;
- 工具描述即说明书,直接决定调用准确率。
