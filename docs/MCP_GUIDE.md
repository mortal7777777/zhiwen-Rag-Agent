# MCP(Model Context Protocol)详解:概念、组成与应用

> 依据 `backend/app/mcp_manager.py`(338 行)与 `agent/langgraph_agent.py` 中
> `mcp_tools()` / MCP null 剔除 / `_mcp_default_args` 逐行核对。
> 项目已接入 Playwright MCP(浏览器自动化),支持 stdio 与 streamable HTTP 两种传输。

---

## 1. 概念:MCP 是什么,解决什么问题

### 1.1 定义

**MCP(Model Context Protocol)= 让 AI 应用接入外部工具的标准化协议**。
Anthropic 于 2024 年开源,类比"AI 世界的 USB-C"或"AI 版 LSP(语言服务器协议)":

```text
┌─────────────┐   MCP 协议(JSON-RPC)   ┌──────────────┐
│  MCP 客户端  │ ◄────────────────────► │  MCP 服务器   │
│ (AI 应用)    │   initialize/list_tools │ (工具提供方)  │
│             │   call_tool/notifications│  如 Playwright│
└─────────────┘                        └──────────────┘
```

### 1.2 为什么需要(对比传统集成)

| 传统方式 | MCP 方式 |
|---|---|
| 每个工具写一套专用集成代码(OpenAI 一套、Claude 一套、本地一套) | 服务器按协议暴露"名称+参数 schema",**任何 MCP 客户端通用** |
| 工具升级 = 改代码重发版 | 服务器独立演进,客户端动态 list_tools 发现 |
| 权限/认证各家实现 | 协议内建(可选) |

**一句话**:MCP 把"工具接入"从"为每家 AI 写适配器"变成"实现一个协议",
工具生态一次开发、处处复用。

### 1.3 两种传输(本项目都支持)

| 传输 | 形态 | 场景 |
|---|---|---|
| **stdio** | 本地子进程:客户端拉起 `npx playwright` 等进程,stdin/stdout 走 JSON-RPC | 本地工具(浏览器/文件系统/数据库) |
| **streamable HTTP** | 远程 HTTP/SSE 端点 | 云端服务(远程知识库、SaaS 工具) |

---

## 2. 组成:MCP 的协议要素

### 2.1 协议消息(JSON-RPC 2.0)

| 消息 | 方向 | 作用 |
|---|---|---|
| `initialize` | 客户端→服务器 | 握手,协商协议版本/能力 |
| `tools/list` | 客户端→服务器 | 获取工具清单(name/description/inputSchema) |
| `tools/call` | 客户端→服务器 | 调用工具,返回结构化内容 |
| `notifications/*` | 双向 | 资源变更/日志等异步通知 |

### 2.2 一个 MCP 工具 = 名称 + 描述 + JSON Schema

```json
{
  "name": "browser_navigate",
  "description": "导航到指定 URL",
  "inputSchema": {
    "type": "object",
    "properties": { "url": {"type": "string", "description": "目标地址"} },
    "required": ["url"]
  }
}
```

**这是 MCP 与 LangChain 工具的天然对齐点**:两者都是"函数 + JSON Schema",
`_schema_to_model` 把 MCP 的 inputSchema 转成 pydantic 模型即可桥接。

---

## 3. 项目实现拆解(mcp_manager.py)

### 3.1 架构

```text
MCPManager(单例,管理全部服务器)
  └─ MCPServerSession × N(每个服务器一个)
       ├─ 独立 asyncio 事件循环线程(异步协议栈,同步调用返回)
       ├─ _connect():启动 stdio/HTTP 客户端 → initialize → list_tools
       └─ call_tool():run_coroutine_threadsafe 提交到该循环,同步等结果
```

- 服务器配置存 MySQL `app_meta.mcp_servers`(JSON 数组:id/name/type/
  command+args 或 url/enabled);
- `service.mcp_tools(db)`:prepare 时按启用配置调用 `MCPManager.configure`
  → 每个工具包装成 `StructuredTool`(`mcp_` 前缀);
- **连接失败自动降级**:单个服务器连不上 → 跳过,不影响主流程;
- 设置页可启停服务器;`refresh()` 后重新加载。

### 3.2 桥接细节(schema → pydantic)

```python
_schema_to_model(input_schema):
  支持 integer/number/boolean/array/object/string
  ★ required 且无 default 的字段 → Field(...)(必填)
  ★ 有 default 的字段 → Field(default=...)         # 尊重 schema 默认值
  # 踩坑:Playwright 部分字段声明 required 但带 default
  # (console_messages.level 等),按必填解析会误拦空参调用
```

### 3.3 运行时兜底(三个真实踩坑的修复)

1. **null 剔除(通用)**:StructuredTool 经 pydantic 解析后,可选字段会变成
   `None`(JSON null);Playwright MCP 服务端(zod `.optional()`)**拒绝显式 null**
   (报 "expected X, received null")→ `_mcp_default_args` 发送前剔除
   `{k: v for k, v in args.items() if v is not None}`(tools 节点也有一份);
2. **默认参数补齐(Playwright schema 缺陷)**:`_MCP_TOOL_DEFAULT_ARGS`:
   - `browser_snapshot` 补 `{target: body, depth: 10, boxes: false}` +
     filename 自动命名(snapshot-{时间戳}.md,防覆盖旧快照);
   - `browser_tabs` 补 `{index: 0, url: ""}`(服务端 zod 拒 null);
   - `browser_wait_for` 补 `{time: 1}`(schema 全可选但服务端要求
     time/text/textGone 至少其一);
   - 原则:**仅缺省时补,显式非 None 值不覆盖**;
3. **空参数兜底**:主 tools 节点对"schema 有必填但调用为空"的工具不浪费
   调用,提示模型补参数;全可选 MCP 工具放行到 invoke 由默认参数兜底。

### 3.4 项目里的 MCP 应用:Playwright 浏览器

- 服务器:`npx playwright`(stdio 子进程,由用户配置);
- 工具:`browser_navigate / browser_type / browser_snapshot / browser_tabs /
  browser_wait_for / browser_screenshot` 等;
- 场景:Agent 打开网页、截图、读取控制台、点击——补足"访问实时网页"能力
  (评测集 l3-agent-003:打开 example.com 返回标题,7.5s 完成);
- **并发注意**:MCPManager 是单例,浏览器会话跨请求共享——两个会话同时
  操作浏览器可能互相踩踏,浏览器类任务不建议并行(编排文档已注明)。

---

## 4. 面试题精讲

### Q1. MCP 是什么?解决了什么问题?
**答法**:标准化 AI 应用接外部工具的协议(JSON-RPC):工具以"名称+参数 schema"
暴露,客户端动态发现调用。解决"每家 AI/每个工具一套适配器"的碎片化。
**加分**:类比 LSP/USB-C;提 Anthropic 2024 开源、社区生态(Playwright/
GitHub/数据库服务器)。

### Q2. MCP 与 Function Calling 是什么关系?
**答法**:Function calling 是模型侧的"输出格式约定"(模型决定调哪个函数、
按 schema 填参数);MCP 是工具侧的"接入协议"。两者互补:模型按 function
calling 产出调用意图,执行层通过 MCP 把调用路由到外部工具。
**加分**:点出本项目把 MCP 工具包装成 LangChain StructuredTool 后,
模型侧统一走 bind_tools 的 function calling,两侧解耦。

### Q3. stdio 和 streamable HTTP 传输怎么选?
**答法**:本地/敏感数据走 stdio(子进程隔离、无网络暴露);远程/多客户端
共享走 HTTP。stdio 启动开销小、天然进程隔离;HTTP 可跨机器、易水平扩展。
**加分**:提 stdio 服务器生命周期跟随客户端;提鉴权在 HTTP 传输下更重要。

### Q4. 接入一个新 MCP 服务器,客户端要改代码吗?
**答法**:不用。协议有 `tools/list` 动态发现,客户端按 schema 转成自己的
工具表示即可。本项目改配置(DB 里加一条服务器配置)重启即生效,零代码。
**加分**:这就是协议标准化的核心价值;提"schema 缺陷需要本地兜底"
(默认参数表)是协议之外的真实工程现实。

### Q5. MCP 工具有什么安全注意点?
**答法**:① 工具能力 = 权限:浏览器/文件/命令类服务器要限制目录与操作;
② 远程 HTTP 服务器要鉴权(Token);③ 模型调用仍走应用的权限门
(HITL/白名单);④ schema 可能声明错误(可选/必填、默认值),需要客户端
兜底策略(null 剔除、默认参数)。
**加分**:结合本项目:Playwright 工具不敏感,但写文件的 MCP 工具若存在,
应并入 is_sensitive_tool 审批门。

---

## 速记卡

- MCP = 工具接入协议(JSON-RPC):initialize → tools/list → tools/call;
- 两种传输:stdio(本地子进程)/ streamable HTTP(远程);
- 桥接:schema → pydantic 模型 → LangChain StructuredTool(`mcp_` 前缀);
- 三个坑:null 剔除、默认参数兜底(Playwright)、空参不浪费调用;
- 单例共享连接;浏览器任务不建议并行。
