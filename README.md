# 个人知识库 RAG 智能助手（LangChain / LangGraph Agent）

> **新会话快速上手**：先读 [docs/PROJECT_HANDOFF.md](docs/PROJECT_HANDOFF.md)
> （项目现状 / 架构 / 环境 / 最近改动 / 待办一次性交底），再看
> [docs/AGENT_COMPARISON.md](docs/AGENT_COMPARISON.md) 与
> [docs/HERMES_STYLE_AGENT.md](docs/HERMES_STYLE_AGENT.md)。

前后端分离的个人知识库 + 智能助手：基于 **LangChain 框架**，Agent 编排层使用
**LangGraph 状态图**（4 节点 + 条件路由）实现按需工具调用，支持个人知识库问答、
联网搜索、视觉识别、本机 Skills 复用、三层对话记忆（MySQL）、预设提示词模板、
多供应商模型管理与可视化设置。

- **后端**：FastAPI + LangChain / LangGraph + OpenSearch + 本地 BGE Embedding/Reranker + DeepSeek / 任意 OpenAI 兼容模型 + SenseNova 视觉 + MySQL
- **前端**：Vue 3 + Vite + Element Plus（浅色/深色主题、打字机流式、引用索引、设置面板）

> 与主流 Agent 应用（Claude Code / Codex / Hermes）的架构与功能对比，见
> [docs/AGENT_COMPARISON.md](docs/AGENT_COMPARISON.md)。

## 核心能力

### Agent 编排（LangGraph）

编排不是手工循环，而是显式状态图（见"架构设计"）：`prepare → agent → tools → (循环) → finalize`。

- 输入框上方开关控制：
  - **知识库检索** `knowledge_base_search`：复用完整 RAG 管道（查询扩展 + 混合检索 + Parent-Child + 本地重排），内置 CRAG 兜底；
  - **联网搜索** `web_search`：provider 可插拔（DuckDuckGo 免 key / Tavily 需 key / 关闭）；
  - **识图** `image_to_text`：主模型无视觉时按需调用 SenseNova；
  - **技能检索** `skill_lookup`：按需取回本机已启用的 Skills 指令（结构化分节）。
- 四种模式：**自动**（模型按需决定）/ **知识库** / **联网** / **不启用**（纯对话）；
- **文件与终端工具**（见下节）：读取自动执行，写/命令敏感操作弹窗人工确认。
- **Send 子代理并行**：计划里有多个“工具型步骤”时，用 LangGraph `Send`
  把每个步骤派给独立上下文的子代理（只读/检索类工具）并行执行，
  各分支返回结论摘要后由 merge 节点合并来源与轨迹、同步任务清单；
  主 Agent 只做拆解与汇总，避免重复检索；
- **知识库写入**：新增 `add_document` 工具（txt/md/csv/json），
  保存文档并增量建索引；属敏感操作，经人工确认后才执行；
- **TodoWrite 任务清单（计划硬约束）**：规划后自动播种任务清单（MySQL 持久化、跨轮跟踪），
  Agent 可经 `todo_update` 增删改查（list/add/complete/remove/set，set 支持整体修订），
  前端计划卡片渲染为可勾选清单，切换会话后自动恢复；
- **每轮自动同步**：工具执行后按计划进度自动勾选已完成的工具型步骤（`todo_update`
  本身不推进计划），模型也可主动 complete/add/remove/set 维护；纯推理/总结步骤
  在收尾时自动补完成，未完成的工具型步骤保留未勾选状态（审计留痕）；
- **计划硬约束**：存在未完成的工具型步骤时，模型不允许提前输出最终回答，
  图路由会把任务推回 agent 继续执行（最多提示 3 次，超限或达调用上限才强制收尾）；
- **可审计**：运行 trace（JSONL）记录每次运行的 todos 快照与 `plan_done_count`；
- **计划-工具映射**：复杂问题先规划，每步解析建议工具，工具调用后回填"已完成 x/y 步、当前步骤、剩余步骤"；
- 工具调用上限（默认 6 次），剩余最后一次时提示"补充检索后必须作答"，超限强制收尾；
- 空参数工具调用兜底（返回"参数缺失"，不浪费调用）；`recursion_limit` 超限时自动兜底收尾；
- **停止生成**：客户端断开后后端感知并置停止信号，只保存问题、不落半截回答；
- **打字机流式**：后端逐 token SSE，前端 28ms 节流 + 积压自适应加速，观感连续不拖沓；
- **SSE 心跳**：工具长执行（10~20s）时发送 `: keepalive` 注释行保活；
- **标题后置**：新会话首 token 不被标题阻塞，后台生成后经 `title` 事件更新；
- **引用溯源**：知识库/联网结果带全局 index，回答用 `[n]` 标注，前端可点击高亮来源卡片；
- 决策透明：前端展示执行计划、工具轨迹（含耗时）、引用来源；后端记录完整 trace。

### 文件与命令执行（类 Claude Code，含 HITL 人工确认）

- 工具集：`list_dir` / `read_file` / `grep_search`（只读，自动执行）；
  `write_file` / `edit_file` / `delete_file` / `bash`（敏感，默认人工确认）；
- **Docker 沙箱**：设置里把“命令沙箱”切到 docker 后，`bash` 在
  `python:3.11-slim` 容器内执行（挂载工作目录、`--network=none` 隔离外网、
  限 512M 内存/1 CPU）；容器根文件系统只读、去 Linux 能力、限进程数，
  可选“工作目录只读”（项目 `:ro` 挂载，写入走内存盘 `/scratch`）；
  `COMMAND_SANDBOX` / `SANDBOX_IMAGE` / `SANDBOX_WORKSPACE_READONLY` 可配置；
- **浏览器工具**：已接入 Playwright MCP（`browser_navigate` / `browser_type` /
  `browser_screenshot` 等），设置页可启停；
- **HITL 流程**：Agent 请求敏感操作时，SSE 推送 `permission_request`，
  前端消息卡片内渲染审批框（展示命令/路径/内容预览），用户**批准**或**拒绝**
  （可附备注），提交到 `POST /api/agent/permission/{id}/resolve`；
  Agent 阻塞等待期间流式连接保持，决定后继续执行（拒绝原因会回传给模型，
  模型向用户解释影响并给出替代方案）；
- **快捷审批交互**：审批卡片即选项卡——数字键 `1/2/3` 直接选择（批准 / 拒绝 /
  批准并永久记住）、`↑↓` 移动高亮、`Enter` 确认、`Esc` 返回输入框；
  出现审批时焦点自动跳到卡片，处理完回到输入框继续打字机输出；
  批准/拒绝后卡片自动折叠为一行紧凑状态条，不占对话空间；
- **记住命令（类 Claude Code "下次不用询问"）**：批准时可选「本会话记住」
  （同会话同命令不再询问）或「永久记住」（写入命令白名单、全局自动放行），
  都可在设置页撤销；
- **失败不烧预算**：工具失败（路径/权限/参数错误、命令非零退出码）不消耗迭代次数，
  并自动注入"换一种方式继续尝试"的引导（先列目录确认现状 → 修正参数 →
  目标不可达时向用户说明并给替代方案）；连续失败超过上限（默认 3 次）才强制收尾。
  这修复了"一次路径错误耗尽 6 次预算导致任务半途而废"的问题；
- **路径容错**：绝对路径落在工作目录内自动接受；落在外部返回可操作错误
  （告知工作目录根 + 改用相对路径 + 修改工作目录的建议）；
- **自动分步规划**：是否需要规划由 Agent 自己判断（多步/涉及文件或检索的复杂任务
  自动拆解），规划结果直接列给用户；执行时每完成一步前端自动打对钩
  （任务清单与计划进度联动），Agent 每完成一个小阶段会先汇报进度再继续，
  全部完成后再给最终总结；不需要用户手动切换任何"计划模式"开关。
- 命令白名单（如 `python, git status`）= **自动放行前缀**，命中无需确认；
  未命中命令在 `ask` 模式下仍弹窗批准。模式可切换为 `allow`（全自动批准，
  等价 Claude Code 的 `--dangerously-skip-permissions`，谨慎使用）；
- 等待确认超时（默认 300s）自动取消，用户停止回答也会自动拒绝并收尾；
- 安全边界：文件操作限制在 `tool_workspace`（默认项目根）内、拒绝路径穿越；
  命令带超时（默认 60s）与输出截断；删除不物理销毁，移入 `.agent_trash/` 可恢复；
  写文件使用原子写入（临时文件 + 替换），避免半截文件；
- 可在设置页「工具与集成」配置工作目录、白名单、确认模式与超时，或关闭总开关。

### 对话记忆与上下文工程（类 DeepSeek 网页端）

- MySQL 持久化会话（`conversations`）与消息（`messages`），左侧会话列表可新建/切换/重命名/删除；
- **会话滚动摘要（软窗口）**：消息数超过 60 条**且/或** token 超预算才压缩；
  预算按模板任务差异化（知识库 24k / 编程写作 36k / 通用 32k）；无论如何保留最近 60 条原始消息；
- **长期事实记忆**：每轮对话自动提取并**分类**（用户画像/喜好偏好/工作项目/重要决定/教训与应对/其他）；
  用户说"记住…"时**强制提取**；可在「记忆」面板查看/编辑/新增/删除；
- **自动整合整理**：活跃记忆达到阈值后自动合并重复、新事实覆盖旧事实、归档过时项，
  生成常驻**用户画像摘要**；
- **按需注入**：用户画像摘要（常驻）+ 语义召回（关键词 n-gram 粗筛 → 向量精排，
  相关性不足用最近记忆补位，按 token 预算裁剪）；
- **分层上下文**：时间/热点 → 画像摘要 → 相关记忆 → 会话摘要 → 图片理解 → 历史（预算裁剪）→ 当前问题。
- **Prompt caching（前缀缓存友好）**：系统提示词拆成"静态核心（模板+工具规则）+
  动态部分（任务清单/文档清单/技能）"，静态核心与历史消息放在最前、跨轮字节级稳定，
  动态上下文统一放到历史之后——命中 DeepSeek 等提供商的自动前缀缓存，
  长会话/多轮工具循环的输入成本大幅下降（token 记录新增 cache_hit/cache_miss 指标可验证）。

### 多供应商模型管理（参考 cc-switch）

- 对话模型与视觉模型各自支持**多个供应商**（OpenAI 兼容），设置页可添加/删除/启停/设为当前；
- API Key 脱敏展示，`****last4` 回传表示"保持不变"；旧 `deepseek_*` / `sensenova_*` 键值自动同步；
- 切换供应商无需重启，立即生效（模型懒加载缓存刷新）。

### RAG 检索质量

- Parent-Child 切分：child（约 220 字，重叠 40）精确检索，parent（段落分组约 600 字）完整上下文；
- 查询扩展：Multi-Query + HyDE + 多轮补全；**简单问题自动跳过扩展**（≤16 字、无复杂词、无指代）；
- 混合检索：kNN + BM25 → RRF 融合 → Small-to-Big 聚合 → 本地 bge-reranker 精排；
- 支持 txt / md / csv / doc / docx / xlsx / pdf / epub；
- 增量索引：文件没变直接复用，只新增只嵌入新文件，修改/删除才全量重建；
- 布局感知 PDF 解析（PyMuPDF4LLM），可选扫描页视觉 OCR（需 SenseNova key）。
- **CUDA 容错**：embedding / reranker 遇到 CUDA 异步错误（unknown error /
  illegal memory access / OOM）时自动降级到 CPU 完成本次检索，
  并在 5 分钟冷却后自动探测 GPU 是否恢复；显卡瞬时故障不会让知识库查询持续失败。
- **提速**：查询扩展并行化（Multi-Query / HyDE / 多轮补全并发）+ 同问题 10 分钟缓存 +
  单次查询上限收敛到 4 条；CRAG 联网兜底只在用户本轮开启“联网”开关时生效，
  纯知识库模式不再静默爬网（实测 KB 单题 17~25s，此前可到 50~218s）。

### 视觉能力（SenseNova）

- 上传/粘贴图片自动识图并注入上下文（主模型无视觉时）；
- `image_to_text` 工具允许 Agent 在对话中按需对任意 base64 图片做 OCR/图表解读。

### 联网可信度

- 每条联网结果自动标注 `credibility`（high / medium / low）与原因：
  政府/教育/权威媒体/学术为 high，门户/技术社区为 medium，
  社交/论坛/个人来源为 low，未知域名提示交叉验证；
- 系统提示词强制要求：引用优先 high，medium 需交叉验证，low 不作为事实依据；
  结论仅来自 low/medium 来源时向用户说明“可信度有限”；
- 网页内容视为外部不可信数据，忽略其中要求执行操作、输出凭据、隐瞒用户的指令。

### 技能（Skills）

- 扫描本机 **Codex / Claude / Hermes**（含 optional-skills）技能，去重后约 146 个；
- 设置页支持搜索、**按功能筛选**（文档/写作/研究/效率/设计/编程/GitHub/数据/媒体/邮件）、来源筛选、
  启用开关与"从本助手移除"（只影响本助手，不修改其他 Agent 的文件）；
- **自动注入**：每轮对话自动把"技能目录"注入系统提示词，并按当前问题关键词
  匹配 Top 2 技能、注入清洗后的分节说明（When to Use / Prerequisites / Steps），
  不需要模型先想起调用 `skill_lookup`；
- Agent 的 `skill_lookup` 只检索已启用技能，返回 **分节目录**；
- **防注入**：技能内容视为不可信参考数据，高风险指令行（覆盖指令、索要凭据、
  绕过审批、外传数据、破坏性命令）会被过滤替换，系统提示词中固化安全边界；
- 默认精选手集 27 个（文档/写作/研究/设计/编程/数据），集成类（notion、
  google-workspace、github 全家桶等）默认关闭，可在设置页按需开启。

### 可观测性

- `agent_runs`：每次运行的 question / plan / tool_trace（含耗时）/ answer_len / latency / status / error / **token 用量**；
- 结构化 trace：`opensearch_meta/traces/YYYY-MM-DD.jsonl`（线程级 token 聚合，含流式调用补记）；
- `/api/metrics`：请求量、错误率、检索/生成/扩展平均耗时；
- 预留 Langfuse 接入点（当前本地 JSONL，隐私友好）。

### 前端

- Vue 3 + Element Plus，浅色/深色/跟随系统主题，内容宽度按窗口比例调节（55%/70%/85%/全宽）；
- 设置面板：居中圆角卡片 + 背景模糊 + 顶部横向导航（外观 / 模型与供应商 / 技能）；
- 欢迎页每日建议（知识库 / 热点新闻 / 通用，每天轮换并缓存）；
- 提示词模板管理、长期记忆管理、知识库上传/重建/删除。

## 架构设计

### 后端分层

```text
api/           FastAPI 路由：agent / chat / conversations / templates / memories /
               documents / vision / settings / skills / suggestions / runs / health
agent/         LangGraph 编排（langgraph_agent）＋ 上下文工程（context）＋ 工具（tools）＋ 提示词（prompts）
rag/           检索管道：embedding / reranker / store(OpenSearch) / retriever /
               query_expander / splitter / loader / service
db/            MySQL 数据层：models / repository（会话、消息、记忆、模板、运行记录）
runtime_config 运行时配置（多供应商、技能偏好，DB 覆盖优先）
tracing        线程级 token 聚合 + JSONL trace
config.py      全局配置（环境变量可覆盖）
```

### LangGraph 编排图

```text
START ──> prepare ──> agent ──> tools ──> agent ──> ...
                        │            ▲
                        └──有工具调用──┘   （循环，直到无工具调用 / 超限 / 停止）
                    无工具调用 / 停止 ──> finalize ──> END
```

- **prepare**：会话/历史/滚动摘要、标题后台线程、计划（含工具映射）、KB 文档清单、
  记忆召回（GPU 锁内）、视觉识别、时间注入，组装分层消息与工具；广播 `session/plan/vision`。
- **agent**：LLM 并发锁内流式生成（token 聚合），有 `tool_calls` 走 tools，否则收尾。
- **tools**：执行工具、回填 `ToolMessage`、注入计划进度、检查调用上限、空参数兜底；广播 `tool_start/tool_result`。
- **finalize**：来源去重、联网附录、持久化消息、运行记录 + trace、标题事件、`done`。

状态通过 `AgentState`（TypedDict）传递；节点经 `EventBus` 推送事件，SSE 协议与旧版一致。
`recursion_limit=30`，超限/异常走兜底收尾，保证有最终回答与运行记录。

### 一次问答的数据流

```text
前端提问 ─> /api/agent/stream
  ─> prepare：会话/摘要/记忆/视觉/计划/时间，组装 messages 与 tools
  ─> agent  ：模型流式生成（token 事件）
        │ 有工具调用
        ▼
     tools：knowledge_base_search（GPU 锁内检索）→ 结果回填 ToolMessage
        │ 回到 agent
        ▼
     agent 无工具调用 ─> finalize：去重/附录/持久化/运行记录/trace ─> done
  ─> 后台：长期记忆提取（显式"记住"时强制）＋ 自动整合
```

## 项目结构

```text
rag_knowledge_base/
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI 入口（MySQL 初始化、路由注册）
│   │   ├── config.py                # 全局配置（环境变量可覆盖）
│   │   ├── runtime_config.py        # 运行时配置（多供应商、技能偏好）
│   │   ├── tracing.py               # token 聚合 + JSONL trace
│   │   ├── schemas.py               # API 请求/响应模型
│   │   ├── api/                     # 路由：health / chat / agent / conversations / templates /
│   │   │                            #        documents / memories / vision / settings / skills /
│   │   │                            #        suggestions / runs
│   │   ├── agent/
│   │   │   ├── langgraph_agent.py   # LangGraph 编排（4 节点 + 条件路由）
│   │   │   ├── agent.py             # AgentService 基础（模型/上下文/视觉懒加载）
│   │   │   ├── context.py           # 记忆/摘要/规划/上下文工程
│   │   │   ├── tools.py             # @tool 工具（KB / Web / Vision / Skill）
│   │   │   ├── prompts.py           # 模板 + 工具规则 + 系统提示组装
│   │   │   └── skills.py            # 本机 Skills 扫描/去重/启停/结构化目录
│   │   ├── db/                      # MySQL：database / models / repository
│   │   └── rag/                     # embedding / reranker / store / retriever / llm /
│   │                                # query_expander / splitter / loader / service
│   ├── run.py                       # 开发启动脚本
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── api/                     # axios + SSE 封装
│   │   ├── views/                   # ChatView（会话+Agent）/ KnowledgeView（知识库）
│   │   ├── components/              # MarkdownContent（渲染 + 引用角标）
│   │   ├── styles/theme.css         # 设计令牌（浅/深主题）
│   │   ├── router/                  # 路由
│   │   └── App.vue                  # 布局 + 设置面板
│   ├── package.json
│   └── vite.config.js               # 开发代理 /api -> 127.0.0.1:8000
├── data/                            # 知识库文档目录（可配置 DATA_DIR）
├── opensearch_meta/                 # 索引账本 + traces/
├── docs/AGENT_COMPARISON.md         # 与主流 Agent 的架构/功能对比
└── README.md
```

## 环境要求

1. **OpenSearch** 已启动（测试环境配置）：

   ```yaml
   network.host: 0.0.0.0
   http.port: 9200
   discovery.type: single-node
   plugins.security.disabled: true
   ```

   本机安装路径：`D:\AI\新建文件夹 (3)\opensearch-3.5.0-windows-x64\opensearch-3.5.0`，
   启动方式：后台运行其 `bin\opensearch.bat`（不是 Docker 容器）。

2. **MySQL** 已启动，能创建数据库/表（默认 `rag_assistant`，首次启动自动建库建表、写入内置模板）。

3. 本地模型（与 day5_2 共用，位于项目上一级的 `local_models/`）：
   - `bge-base-zh-v1.5`（Embedding，768 维，CUDA 下默认 fp16）
   - `models--BAAI--bge-reranker-v2-m3`（Reranker）

4. Python 3.10+ 与 Node.js 18+（依赖含 `langgraph`，见 `backend/requirements.txt`）。

## 快速开始

### 1. 安装依赖

```powershell
cd backend
pip install -r requirements.txt
cd ..\frontend
npm install
```

### 2. 配置环境变量（PowerShell 示例）

```powershell
# 必填：对话模型 API Key（OpenAI 兼容，可在设置页改供应商）
$env:DEEPSEEK_API_KEY = "sk-..."

# 可选：视觉模型（SenseNova Token Plan，识图/OCR）
$env:SENSENOVA_API_KEY = "sk-..."

# 可选：MySQL 连接串（默认 root 空密码连本机 rag_assistant 库）
$env:MYSQL_URL = "mysql+pymysql://root:你的密码@127.0.0.1:3306/rag_assistant?charset=utf8mb4"
```

> 注意：`SENSENOVA_API_KEY` 未写入注册表，重启后端时需在启动它的终端里保留该环境变量。

### 3. 启动

```powershell
# 后端（127.0.0.1:8000，接口文档 /docs）
cd backend
python run.py

# 前端（http://localhost:5173）
cd ..\frontend
npm run dev
```

## 测试

```powershell
cd backend
pip install -r requirements-dev.txt
python -m pytest tests -q
```

当前 29 个用例覆盖：任务清单（播种/同步/跨轮刷新/模糊完成/修订/进度）、文件工具
安全边界与命令白名单、CUDA 降级逻辑、技能内容清洗、联网可信度标注、
RAG 工具纯函数。测试不依赖 GPU / MySQL / 网络。

提交前自动跑测试：已安装 pre-commit 钩子（`scripts/install-git-hooks.ps1`），
`git commit` 前会自动执行 pytest，失败则阻止提交（`--no-verify` 可跳过）。

## 评测

- `backend/eval_questions.json`：14 题种子集（知识库/通用/联网/工具/安全/规划）；
- `backend/evaluate_agent.py`：逐题调用 Agent 流式接口，记录延迟/工具/计划/
  token/状态到 `eval_report.jsonl`，改动前后对比可发现回归；
- `backend/eval_ragas.py`：RAGAS `faithfulness` 基线（DeepSeek 裁判），
  结果写入 `ragas_baseline.jsonl`（基线示例：kb-001 faithfulness=1.0）；
- 评测方案与开源集建议（RAGAS / BEIR / C-MTEB / GAIA / AgentBench）见
  [docs/EVALUATION.md](docs/EVALUATION.md)。

## 环境变量（均有默认值）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 空（必须设置） | 对话模型 API Key |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 对话模型名 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容接口地址 |
| `MAIN_MODEL_VISION` | `0` | 主对话模型是否原生支持视觉 |
| `SENSENOVA_API_KEY` | 空 | SenseNova Token Plan Key（识图/OCR） |
| `SENSENOVA_BASE_URL` | `https://token.sensenova.cn/v1` | 视觉模型接口 |
| `SENSENOVA_MODEL` | `sensenova-6.8-flash-lite` | 视觉模型名 |
| `VISION_AUTO_DESCRIBE` / `VISION_MAX_IMAGES` | `1` / `6` | 自动识图开关 / 单次最多图片数 |
| `MYSQL_URL` | `mysql+pymysql://root:@127.0.0.1:3306/rag_assistant?charset=utf8mb4` | MySQL 连接串 |
| `WEB_SEARCH_PROVIDER` | `duckduckgo` | `duckduckgo` / `tavily` / `off` |
| `TAVILY_API_KEY` | 空 | 使用 Tavily 时填写 |
| `WEB_SEARCH_MAX_RESULTS` | `6` | 单次联网搜索结果条数 |
| `AGENT_MAX_ITERATIONS` | `6` | 工具调用循环上限 |
| `AGENT_SUBAGENTS_ENABLED` | `1` | Send 子代理并行总开关 |
| `AGENT_SUBAGENT_MAX_ROUNDS` | `2` | 每个子代理最多 LLM 轮数 |
| `VERIFY_COMMAND` | 空 | 写/改文件后自动运行的验证命令（显式配置优先，空=按类型自动检测） |
| `VERIFY_AUTO_DETECT` | `1` | 未配置 VERIFY_COMMAND 时按扩展名自动验证（.py→py_compile / .js→node --check / .json/.yaml 语法） |
| `VERIFY_MAX_RETRIES` | `1` | 验证失败后允许模型继续修复并复验的次数，超过则要求如实说明 |
| `CHECKPOINT_NATIVE_ENABLED` | `1` | LangGraph 原生 checkpointer（快照时间线） |
| `AGENT_MAX_FAILURES` | `3` | 工具失败重试上限（失败不占迭代预算） |
| `AGENT_RECURSION_LIMIT` | `30` | LangGraph 图执行最大步数 |
| `CHAT_TEMPERATURE` | `0.5` | 回答温度 |
| `AGENT_TITLE_MODEL` | `deepseek-v4-flash` | 标题生成模型 |
| `HISTORY_MAX_MESSAGES` | `60` | 历史消息软窗口条数 |
| `HISTORY_MAX_TOKENS` | `32000` | 历史消息 token 预算 |
| `SUMMARY_ENABLED` / `SUMMARY_MAX_CHARS` | `1` / `800` | 滚动摘要开关 / 最大字符 |
| `MEMORY_ENABLED` / `MEMORY_TOP_K` | `1` / `3` | 长期记忆开关 / 每次注入条数 |
| `MEMORY_MIN_SCORE` / `MEMORY_MAX_TOKENS` | `0.35` / `600` | 记忆召回阈值 / token 预算 |
| `MEMORY_CANDIDATE_LIMIT` / `MEMORY_RECENT_FALLBACK` | `100` / `50` | 记忆粗筛候选 / 最近补位 |
| `MEMORY_CONSOLIDATE_INTERVAL_HOURS` / `MEMORY_CONSOLIDATE_THRESHOLD` | `24` / `10` | 自动整合间隔 / 触发条数 |
| `PLANNER_ENABLED` | `1` | 复杂问题规划 |
| `CRAG_FALLBACK_ENABLED` / `CRAG_MIN_SCORE` | `1` / `0.45` | 知识库不足时补联网 |
| `QUERY_EXPANSION_ENABLED` / `EXPANSION_*` | `1` | 查询扩展各开关（含简单问题自动跳过） |
| `PARENT_CHUNK_SIZE` / `PARENT_MAX_CHUNK_SIZE` | `600` / `900` | Parent 切分参数 |
| `CHILD_CHUNK_SIZE` / `CHILD_OVERLAP` | `220` / `40` | Child 切分参数 |
| `MAX_PARENTS` | `6` | 返回给模型的 Parent 数量 |
| `RECALL_K` / `CANDIDATE_POOL` / `RERANK_TOP_K` | `40` / `24` / `4` | 召回/融合/精排参数 |
| `PDF_LAYOUT_ENABLED` / `PDF_VISION_OCR_ENABLED` | `1` / `0` | 布局 PDF 解析 / 扫描件视觉 OCR |
| `EMBEDDING_FP16` / `EMBED_BATCH_SIZE` | `1` / `128` | 嵌入精度 / 批大小 |
| `MAX_CONCURRENCY` | `4` | GPU（检索/重排）并发上限 |
| `LLM_MAX_CONCURRENCY` | `8` | LLM API 调用并发上限 |
| `TRACING_ENABLED` | `1` | JSONL trace 开关 |
| `ADVANCED_TOOLS_ENABLED` | `1` | 文件/命令受控执行工具总开关 |
| `TOOL_WORKSPACE` / `COMMAND_ALLOWLIST` | 空 | 文件工作目录（空=项目根）/ 命令自动放行前缀 |
| `COMMAND_TIMEOUT` | `60` | 命令执行超时（秒） |
| `COMMAND_SANDBOX` | `subprocess` | 命令执行环境：`subprocess`（本机）/ `docker`（容器沙箱） |
| `SANDBOX_IMAGE` | `python:3.11-slim` | Docker 沙箱镜像 |
| `TOOL_PERMISSION_MODE` | `ask` | 敏感操作确认：`ask`（每次确认）/ `allow`（自动批准） |
| `PERMISSION_TIMEOUT` | `300` | 等待人工确认超时（秒），超时自动取消 |
| `OPENSEARCH_URL` / `OPENSEARCH_INDEX` | `http://localhost:9200` / `rag_knowledge_base_v2` | OpenSearch 地址 / 索引名 |
| `DATA_DIR` / `META_DIR` | `rag_knowledge_base/data` / `opensearch_meta` | 文档目录 / 账本目录 |
| `EMBEDDING_MODEL_DIR` / `RERANKER_CACHE_DIR` | `../local_models/...` | 本地模型目录 |
| `CORS_ORIGINS` | `http://localhost:5173,...` | 前端跨域白名单 |

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| POST | `/api/agent/chat` | Agent 问答（JSON：question / conversation_id / tool_mode / template_id / images） |
| POST | `/api/agent/stream` | Agent 流式问答（SSE：session / plan / vision / tool_start / tool_result / token / title / done / error；空闲 15s 发 `: keepalive`） |
| POST | `/api/agent/permission/{id}/resolve` | 人工确认：批准/拒绝敏感操作（写文件/编辑/删除/执行命令） |
| GET | `/api/agent/permissions` | 当前待人工确认的审批请求列表 |
| GET/PUT | `/api/todos/{conversation_id}` | 读取 / 保存某会话的 TodoWrite 任务清单 |
| GET/POST | `/api/conversations` | 会话列表 / 新建 |
| PATCH/DELETE | `/api/conversations/{id}` | 重命名 / 删除会话 |
| GET | `/api/conversations/{id}/messages` | 会话历史消息 |
| GET/POST | `/api/templates` | 模板列表 / 新建 |
| PATCH/DELETE | `/api/templates/{id}` | 编辑 / 删除自定义模板 |
| GET/POST | `/api/memories` | 长期记忆列表 / 新增 |
| PATCH/DELETE | `/api/memories/{id}` | 编辑 / 删除记忆 |
| POST | `/api/memories/consolidate` | 手动整合整理记忆 |
| GET | `/api/documents` | 知识库文档列表 |
| POST | `/api/documents/upload` | 上传文档（multipart） |
| DELETE | `/api/documents/{path}` | 删除文档并重建索引 |
| POST/GET | `/api/index/rebuild` / `/api/index/status` | 重建索引 / 索引状态 |
| GET/PUT | `/api/settings` | 读取（脱敏）/ 保存运行时配置（供应商、温度、联网、技能开关） |
| GET/PUT | `/api/hooks` | 生命周期 hooks 配置（PreToolUse / PostToolUse 用户脚本回调） |
| GET | `/api/conversations/{id}/timeline[/{checkpoint_id}]` | 原生 checkpointer 快照时间线 / 快照详情 |
| GET | `/api/skills` | 技能列表（含启停/隐藏状态与偏好） |
| GET | `/api/skills/search` | 按语义/关键词检索技能 |
| PUT | `/api/skills` | 保存技能偏好（enabled / hidden） |
| POST | `/api/skills/reset` | 恢复默认精选手集 |
| GET | `/api/suggestions` | 每日建议（知识库 / 热点 / 通用） |
| GET | `/api/runs` | Agent 运行记录（决策可观测性） |
| GET/POST | `/api/vision/status` / `/api/vision/describe` | 视觉状态 / 识图 |
| GET | `/api/vision/models` | 视觉平台模型列表 |
| POST | `/api/chat` / `/api/chat/stream` | 旧版 RAG 问答（兼容保留） |
| GET | `/api/metrics` | 监控指标（请求量 / 错误率 / 平均耗时） |

## 注意事项

- **版本保护**：项目已初始化 git（仓库根 = `rag_knowledge_base/`），
  基础操作见 [docs/GIT_GUIDE.md](docs/GIT_GUIDE.md)；运行数据（`data/`、
  `opensearch_meta/`、日志）已加入 `.gitignore` 不入库；
- **提示词模板**：预设模板已按 Claude Code 风格重写（角色/原则/流程/安全边界），
  含“学习助手”模板；后端启动时自动同步到数据库（系统模板只读，用户模板不受影响）；
- **MySQL 直启**：`python run.py` 会读取 gitignore 的 `backend/.env.local`
  （本机 MYSQL_URL）；或使用 `start_backend.ps1`；
- **SenseNova DeepSeek-V4 flash**：base_url 必须带 `/v1`（已修正）；
  免费档有 RPM/额度限流，超限时请在设置中切回 DeepSeek 官方供应商；
- **长期记忆提取**：默认只对“≥400 字回答或显式‘记住’”的对话做提取
  （`MEMORY_AUTO_EXTRACT_MIN_CHARS` 可调），短问答不再每轮多花一次 LLM 调用；
- **MySQL 未连接时自动降级**：普通对话仍可用，但会话记忆、模板、运行记录不可用（日志给出明确错误）；
- 联网搜索默认 DuckDuckGo（免 key），国内网络可能需要代理；也可配置 Tavily；
- 问答必须配置对话模型 API Key（可在设置页添加/切换供应商）；知识库检索与重排序全程本地；
- 首次运行或删除文件后会全量嵌入，耗时取决于文档量；上传新文件只做增量追加；
- EPUB 本质是 ZIP 压缩包（XHTML 章节 + OPF 元数据），用 ebooklib + BeautifulSoup 按章节解析；
- 老式二进制 `.doc` 使用 `doc2txt`（内置 antiword）解析，失败时回退 `legacy-doc`；
- 技能"移除"只影响本助手（skill_lookup 不再使用），不会修改其他 Agent 的技能文件。
