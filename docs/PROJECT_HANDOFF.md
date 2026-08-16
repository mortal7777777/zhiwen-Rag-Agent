# 项目交接文档（供新会话快速上手）

> 最后更新：2026-08-14（第四轮：run_hooks 漏导入致命修复 + CLI Ctrl+C 加固，见 §4.14）
> 目的：把项目现状、架构、环境、最近改动与待办一次性交底，新会话先读本文件即可工作。

## 0. 一句话定位

**个人知识库 RAG + 智能助手**：Vue3 + Element Plus 前端、FastAPI 后端、LangChain 消息抽象 + **LangGraph 四节点编排**的 agent。用户在从零学习 agent / Claude Code / Hermes 的架构，整个项目**必须用 LangChain 框架**，偏好本地优先、工程化、可观测，风格是“先做能演示的”。

---

## 1. 环境与运行（当前都在跑）

| 项 | 值 |
|---|---|
| 项目根 | `C:\Users\user\PycharmProjects\PythonProjectPytorch1\langchain01\rag_knowledge_base` |
| 后端 | FastAPI + uvicorn，`0.0.0.0:8000`，**PID 3096**（重启后 PID 会变） |
| 前端 | Vite + Vue3，dev server `::1:5173`，**PID 46732**（重启后 PID 会变） |
| Python | `D:\conda_envs\pytorch_env\python.exe`（带 torch/cuda） |
| MySQL | `mysql+pymysql://root:CHANGE_ME@127.0.0.1:3306/rag_assistant?charset=utf8mb4`，库名 `rag_assistant` |
| OpenSearch | `http://localhost:9200`，索引 `rag_knowledge_base_v2` |
| 本地模型 | `langchain01/local_models/bge-base-zh-v1.5`（embedding）+ `models--BAAI--bge-reranker-v2-m3`（reranker），CUDA + fp16 |
| 对话模型 | DeepSeek（默认 `deepseek-v4-flash`，OpenAI 兼容，key 在环境变量 `DEEPSEEK_API_KEY`） |
| 视觉模型 | SenseNova Token Plan（`SENSENOVA_API_KEY`，`sensenova-6.8-flash-lite`） |
| 数据/账本 | `rag_knowledge_base/data`（上传文档）、`rag_knowledge_base/opensearch_meta`（索引账本 + traces JSONL） |

**后端重启命令（必须注入环境变量，端口绑定需管理员权限）**：
```powershell
$env:MYSQL_URL='mysql+pymysql://root:CHANGE_ME@127.0.0.1:3306/rag_assistant?charset=utf8mb4'
$env:SENSENOVA_API_KEY=(从用户环境变量读取)
$env:DEEPSEEK_API_KEY=(从用户环境变量读取)
Start-Process D:\conda_envs\pytorch_env\python.exe -ArgumentList '-m','uvicorn','app.main:app','--host','0.0.0.0','--port','8000' -WorkingDirectory '...\backend' -WindowStyle Hidden -RedirectStandardOutput backend\uvicorn.out.log -RedirectStandardError backend\uvicorn.err.log
```
> 沙箱内直接启动会因 `WinError 10013` 失败；即使沙箱内 `Start-Process` 启动成功，
> 子进程也会**继承沙箱的网络限制**（出站连模型 API 同样报 `WinError 10013`），
> 因此重启后端务必**脱离沙箱/提权执行**。

**前端**：`cd frontend && npm run dev`（Vite HMR，改前端代码自动生效；`npm run build` 验证构建）。

---

## 2. 架构总览

### 后端分层（`backend/app/`）

```text
api/            FastAPI 路由：agent/chat/conversations/templates/memories/documents/
                vision/settings/skills/suggestions/runs/health/advanced
agent/          LangGraph 编排（langgraph_agent.py）、上下文工程（context.py）、
                提示词（prompts.py）、工具（tools.py）、记忆（memory.py）
rag/            embeddings / retriever / reranker / splitter / store / loader / service /
                query_expander / vision（SenseNova 识图）
db/             SQLAlchemy：models / repository / database（会话、消息、模板、记忆、运行记录、app_meta）
permissions.py  HITL 审批管理器（人工确认核心）
tools_extra.py  文件/命令工具集（类 Claude Code）
todos.py        TodoWrite 任务清单（按会话持久化）
checkpoint.py   SQLite 快照（中断恢复）
project_memory.py  AGENTS.md 文件型项目记忆
trajectory.py  长任务轨迹摘要压缩
monitoring.py / tracing.py  指标与 JSONL trace
runtime_config.py  运行时可改配置（供应商/开关，DB 覆盖优先）
config.py       全部默认配置 + 环境变量
```

### LangGraph 编排（`agent/langgraph_agent.py`）

```
START -> prepare -> agent -> tools -> (循环) -> finalize -> END
```

- **prepare**：会话历史/滚动摘要/长期记忆/任务清单(todos)/规划/时间/视觉描述/工具组装 → 分层上下文。
- **agent**：`chat.bind_tools(tools)` 流式生成；有 `tool_calls` 走 tools，否则收尾；工具调用上限（默认 6）时强制收尾。
- **tools**：执行工具；敏感工具先 HITL 审批（`permissions.py`）；失败不烧预算（`failure_count` 上限 3 才强制收尾）；回填计划进度。
- **finalize**：来源去重、持久化消息、运行记录 + trace、标题后置、长期记忆提取。

**关键坑（务必记住）**：
- LangGraph 节点里直接改 `state` 的字段不生效，必须把改动**放进节点返回值 dict**（如 `tool_calls_used / forced_final / last_call_warned` 都这么传）。
- HITL 审批在 tools 节点内**阻塞 worker 线程**等待用户决定；SSE 靠 15s keepalive 保活；客户端断开时 `stop_event` 置位会自动拒绝并收尾。
- SSE 事件协议：`session / plan / plan_progress / todos / status / reasoning / vision / tool_start / tool_result / permission_request / permission_resolved / token / title / done / error`；`api/index.js` 的 `streamAgentChat` 按此解析。

### 前端（`frontend/src/`）

- `App.vue`：布局 + 设置面板（外观/模型与供应商/技能/工具与集成四页签，居中圆角卡片 + 顶部横向导航）。
- `views/ChatView.vue`：对话主界面（左侧会话列表可折叠；消息卡片：计划/任务清单/审批卡/工具轨迹/引用来源；输入区开关：自动/联网/知识库 + 模板选择；打字机 24ms/帧；流式期间纯文本渲染）。
- `views/KnowledgeView.vue` / `RunsView.vue`：知识库管理 / 运行记录。
- `components/MarkdownContent.vue`：marked + DOMPurify 渲染；`plain` 模式=流式纯文本；代码块悬浮复制/终端执行。
- `styles/theme.css`：CSS 变量主题（深/浅色）。

---

## 3. 核心能力现状

### RAG
- Parent-Child 切分（child ~220 字/重叠 40，parent 段落分组 ~600 字）；查询扩展（Multi-Query + HyDE + 多轮补全，简单问题自动跳过）；kNN + BM25 → RRF → 本地 bge-reranker 精排；CRAG 兜底。
- 支持 txt/md/csv/doc/docx/xlsx/pdf/epub；增量索引；布局感知 PDF；扫描件可选 SenseNova OCR。

### Agent（最近的主战场）
- **文件/命令工具（类 Claude Code）**：只读 `list_dir / read_file / grep_search` 自动执行；敏感 `write_file / edit_file / delete_file / bash` 默认弹审批（HITL）。
- **HITL 审批**：`permissions.py`；SSE `permission_request` → 前端审批卡（数字键 1/2/3、↑↓、Enter、Esc）→ `POST /api/agent/permission/{id}/resolve`；批准后卡片折叠为状态条；拒绝原因回传模型。
- **记住命令**：批准时可选“本会话记住”（`remember_session`）或“永久记住”（写入 `command_allowlist`）；实测 whoami 第二次不再询问。
- **失败重试**：工具失败（error 或命令非零退出码）**不消耗迭代预算**，注入“换方式继续”引导；连续失败 `AGENT_MAX_FAILURES`（默认 3）才强制收尾；路径错误信息可操作化（绝对路径在工作目录内自动接受，外部给出建议）。
- **自动分步规划**：agent 自己判断是否规划（多步任务自动拆解，文件/命令任务也规划）；计划列给用户，执行时前端自动打对钩（todo 与 planDone 联动）；已移除“手动计划模式”开关（用户明确否决）。
- **TodoWrite 任务清单（计划硬约束）**：`todos.py` 按会话持久化到 MySQL app_meta；
  `todo_update` 工具支持 list/add/complete/remove/set（set=整体修订）；
  `GET/PUT /api/todos/{conversation_id}`；前端可勾选；切换会话恢复。
  计划硬约束：未完成的工具型步骤存在时，路由会把模型推回 agent 继续执行
  （最多 3 次提示），不允许提前收尾；每轮工具执行后自动同步勾选已完成步骤
  （`todo_update` 本身不推进计划），纯推理/总结步骤在收尾时自动补完成，
  未完成的工具型步骤保留未勾选（审计留痕）；trace 记录 todos 快照与 plan_done_count。
- **记忆体系**：MySQL 会话/消息；滚动摘要（软窗口，60 条/32k tokens 预算）；长期事实记忆分类提取 + 自动整合 + 画像摘要 + 语义召回注入；`AGENTS.md` 文件型项目记忆。
- **深度思考摘要**：工具执行期间后台预生成 ≤120 字摘要，`reasoning` 事件 → 前端“已深度思考”折叠区（原始 CoT 不展示）。
- **可观测**：`agent_runs` 表 + JSONL trace；运行记录页；token 用量记录。
- **终端客户端** `backend/cli_agent.py`：纯标准库、类 Claude Code 交互；流式 Markdown 渲染（标题/列表/引用/代码块/表格）、工具卡片+耗时、spinner、带边框数字键审批弹窗、状态栏（会话/模式/耗时/tokens）；↑/↓ 历史、Ctrl+C/Esc 即时打断（调用 `/api/agent/cancel/{run_id}` 让后端立刻收尾）、Ctrl+L 清屏、Ctrl+R 重发、Tab 补全；历史持久化到目录 `.myragagent_history.json`。

### 设置页当前值（重要）
- `advanced_tools_enabled = true`（受控执行总开关，默认开）。
- `tool_permission_mode = "ask"`（ask=每次确认 / allow=全自动批准）。
- `command_allowlist = "python, dir, echo"`（白名单命令自动放行，其余弹审批）。
- `tool_workspace = ""`（空=项目根 `rag_knowledge_base/`；bash 默认也在工作目录根执行）。
- `permission_timeout = 300`（审批超时自动取消）。

---

## 4. 本会话（最近一轮）做了什么

### 4.1 本轮（todo 硬约束 + 修复两个真实 bug）
1. **todo 模块升级**：任务项区分 `source=plan/manual` 与 `step_index`，
   `plan_progress` 精确计算计划进度；`todo_update` 新增 `set`（整体修订清单）。
2. **计划硬约束落地**：`_route_after_agent` 支持 `agent -> agent` 回推，
   存在未完成工具型步骤时模型不能提前收尾（最多提示 3 次）；
   系统提示词增加硬约束规则（每步 complete、未完成不总结、remove 要说明原因）。
3. **每轮自动同步**：tools 节点按“成功且非 todo_update 的工具调用”推进计划进度，
   自动勾选工具型步骤；收尾自动补完成纯推理/总结步骤；
   前端 `planTotal` 与后端 total 对齐（模型 set/remove 修订后进度不错位）。
4. **修复 bug A**：`_tools_node` 未定义局部 `db`，把模块级 `app.db` 包当 Session
   传入 `load_todos`（`module 'app.db' has no attribute 'get'`）。
5. **修复 bug B**：`todo_update` 在线程池并行执行时共用请求级 Session，
   并发写 MySQL 导致连接损坏（packet sequence / protocol error）；
   改为每个工具调用使用独立短会话（`SessionLocal`），请求级 Session 不再被并发触碰。
6. **可观测**：trace 新增 `todos` 快照与 `plan_done_count`；README 同步更新。

### 4.2 本轮（CUDA 容错 + git + pytest）
1. **CUDA 异步错误容错**：知识库查询偶发 `CUDA error: unknown error`
   （显卡瞬时故障 / 内核上下文污染，一旦出现会持续失败直到进程重启）。
   修复：embedding 与 reranker 识别 CUDA 类异常后**自动降级 CPU** 完成本次检索，
   并设置 300 秒冷却，之后自动探测 GPU 是否恢复（恢复即切回 GPU）。
   实测显卡恢复后知识库查询 4.5s 正常返回。
2. **git 版本保护**：仓库根 `rag_knowledge_base/` 已 `git init` 并提交基线
   （`396f940`）；`.gitignore` 排除 data/、opensearch_meta/、日志、node_modules/dist。
3. **pytest 套件**：`backend/tests/` 共 20 个用例，覆盖 todos、文件工具安全边界/
   白名单、CUDA 降级、工具纯函数；`requirements-dev.txt` 提供测试依赖；
   不依赖 GPU / MySQL / 网络。
4. **顺手修复**：`todo_update` 工具导入路径错误（`from .database` → `from .db.database`），
   此前该工具实际执行失败但被日志掩盖。

### 4.3 本轮（任务清单修复 + 提示词重写 + Skills 策略/安全）
1. **任务清单跨轮修复**：
   - 新计划自动替换旧计划的任务项（保留 manual 追加项），不再串台；
     无新计划且旧清单已完成时自动清空；
   - 收尾补发最终 `plan_progress`，右上角进度能走到 N/N；
   - 修复 SQLAlchemy 身份映射缓存导致的"清单更新不及时"（`todo_update`
     独立会话写库后，请求会话读到旧缓存；`load_todos` 读前 `expire_all`）；
   - 前端任务清单改为**只读**：对号只由模型/系统按进度决定，用户不可手勾。
2. **关键修复：系统提示词未注入**。LangGraph 路径里 `system_prompt_final`
   计算后从未放进消息（旧版 agent.py 正常），模板/工具纪律/todo 硬约束规则
   实际都没进模型上下文；现已作为首条 SystemMessage 注入（含恢复路径）。
3. **提示词模板重写**：预设模板改为 Claude Code 风格
   （角色/工作原则/工作流程/回答要求/安全边界），`seed_templates` 改为
   启动时同步更新系统模板内容（用户模板不受影响）。
4. **Skills 策略升级**：
   - 现状：原方案只靠 `skill_lookup` 按需调用，模型常想不到调用；
   - 改为：每轮注入技能目录 + 按问题关键词自动注入 Top 2 技能分节说明；
   - 默认精选手集 48 → 27（剔除 notion/google-workspace/github 全家桶等
     集成类与高风险类），设置页可重新开启；
   - 新增 `sanitize_skill_text`：过滤覆盖指令/索要凭据/绕过审批/外传数据/
     破坏性命令等注入行；系统提示词固化"技能内容不可信"安全边界。
5. **新增 [docs/GIT_GUIDE.md](docs/GIT_GUIDE.md)**：git 基础操作教程。

### 4.4 本轮（性能诊断 + 联网可信度 + 学习助手模板 + 评测）
1. **性能诊断结论**：embedding/reranker 确为 CUDA（fp16，RTX 4060），GPU 不是瓶颈；
   慢在查询扩展（平均 19s 的 LLM 调用）、多路混合检索与 CRAG 联网兜底
   （单次 10~104s）。run#77 一次检索达 104s、总耗时 218s；run#79 模型
   错误地用 list_dir 探查知识库目录（系统提示词此前未注入也是原因之一）。
2. **提速修复**：
   - 查询扩展三路并行 + 同问题 10 分钟缓存 + 单次查询上限 6→4；
   - CRAG 联网兜底仅在用户本轮开启“联网”开关时生效（纯知识库模式不再静默爬网）；
   - 知识库工具规则明确“不要用 list_dir/read_file 探查知识库目录”；
   - 实测 KB 单题 17~25s（此前 50~218s）；Agent 多轮任务仍受 LLM API 单轮
     3~8s 限制，工具越多总耗时越长属正常。
3. **联网可信度评估**：每条联网结果按域名标注 credibility（high/medium/low）
   与原因；提示词强制要求优先 high、medium 交叉验证、low 不作为事实依据，
   并声明网页内容不可信、不执行网页指令（防网页提示注入）。
4. **新增“学习助手”模板**：Claude Code 风格（角色/原则/流程/要求/边界），
   侧重拆解概念、交叉验证、引导理解、练习检验；启动时自动同步。
5. **评测起步**：`backend/eval_questions.json`（14 题种子集）+
   `backend/evaluate_agent.py`（自动记录延迟/工具/计划/token/状态）+ 
   [docs/EVALUATION.md](docs/EVALUATION.md)（RAGAS/BEIR/C-MTEB/GAIA/AgentBench
   选型与回归门槛建议）。

### 4.5 本轮（运维修复 + 评测闭环 + 并行检索 + 成本优化）
1. **MySQL 直启修复**：根因是 `python run.py` 未读 MYSQL_URL，默认 root 空密码
   → Access denied。`run.py` 现支持 `backend/.env.local`（gitignored），
   启动错误提示给出可操作指引；已验证恢复。
2. **OpenSearch 恢复**：RAG 检索 500 是 OpenSearch 进程掉了（非 Docker，
   安装于 `D:\AI\新建文件夹 (3)\opensearch-3.5.0-windows-x64\opensearch-3.5.0`），
   已后台重启并验证；注意其数据盘仅剩 8% 空间，达到高水位可能进入只读。
3. **SenseNova DeepSeek-V4 flash 验证**：模型可用，但 base_url 需带 `/v1`
   （已修正配置）；免费档有 RPM/额度限流（实测 429 rpm exhausted），
   当前主对话供应商已切回 DeepSeek 官方，额度恢复后可再切。
4. **评测闭环**：pre-commit 钩子（pytest，失败阻止提交）+ `eval_ragas.py`
   RAGAS faithfulness 基线（DeepSeek 裁判，kb-001=1.0，写入 ragas_baseline.jsonl）。
5. **并行检索 fan-out**：知识库+联网同时开启且计划同时需要两者时，
   新增 fanout 节点并行执行两种检索再汇总（路由逻辑已单测）。
6. **成本优化**：记忆提取条件化（默认 ≥400 字回答或显式“记住”才提取，
   `MEMORY_AUTO_EXTRACT_MIN_CHARS` 可调）；查询扩展缓存此前已完成。
7. **#3 沙箱/浏览器现状**：Docker 客户端已装但 Desktop 未运行；Playwright 未安装。
   待 Docker Desktop 启动后再接 Playwright MCP 与 bash 沙箱。

### 4.6 本轮（全量评测基线 + 钩子修复 + 编排测试 + add_document）
1. **全量 19 题评测基线**（`evaluate_agent.py --approve`）：19/19 status=ok，
   总耗时约 11.3 分钟、合计 687,475 tokens；L1/L2/L3 平均耗时
   11.6s / 34.6s / 53.1s，明细见 docs/EVALUATION.md。
2. **pre-commit 钩子修复**：`--basetemp .git/pytest-tmp` + `-p no:cacheprovider`，
   解决非管理员环境 Temp 目录 WinError 5；钩子已实测放行。
3. **编排层测试**：新增子代理拆分/来源重编号/计划硬约束 5 个单测（共 37 个）。
4. **RAGAS 基线修复**：裁判换 `deepseek-chat`（max_tokens 8192），
   修复 NaN；6 道知识库题全有效，faithfulness 均值约 0.975。
5. **知识库写入**：新增 `add_document` 工具（人工确认后写文件+增量建索引）。
6. **审批自动化竞态**：`--approve` 改为跑前切 `allow`/跑后恢复 `ask`，
   避免 SSE 审批事件延迟导致的 resolve 404。

### 4.7 本轮（原生 checkpointer 时间线 + hooks + 子代理 HITL/verify）
1. **LangGraph 原生 checkpointer**：`langgraph-checkpoint-sqlite` +
   SafeJsonPlusSerializer；每个 superstep 自动落快照，新增
   `GET /api/conversations/{id}/timeline[/{checkpoint_id}]` 时间线审计；
   新会话提前建号对齐 thread_id；自写 checkpoint 仍负责跨轮恢复。
2. **生命周期 Hooks**：`hooks.py` + `GET/PUT /api/hooks`；
   PreToolUse 可 deny 拦截、PostToolUse 回填 additional_context；
   SSE 新增 `hook` 事件，前端有状态提示。
3. **子代理**：file/bash 子代理带写/编辑/删除/命令工具且走 HITL；
   `agent_subagent_max_rounds` 预算；写后 `verify_command` 自动验证回填。

### 4.8 本轮（缓存指标补全 + 快照回滚 + verify/hooks 加固）
1. 流式主路径补记缓存命中/未命中 token（兼容 DeepSeek/OpenAI 字段），
   agent_runs 可见；
2. 快照回滚：`POST /api/conversations/{id}/timeline/{checkpoint_id}/rollback`
   重建消息历史与任务清单，下次提问从该状态继续（实测恢复 2 条消息）；
3. JSON/YAML 验证命令去除嵌套引号（json.tool / 无 print 的 python -c）；
4. hooks 改为 `shlex.split` + `shell=False`，避免 shell 注入；测试增至 53 个。

### 4.9 本轮（项目级任务独立限流 + 终端客户端优化）
1. **task_mode**：自动识别“完成/实现/开发/搭建/重构/整个项目”等任务，
   工具预算 6→24、失败上限 3→6（`AGENT_TASK_MAX_*` 可调）；
   预算用尽时输出进度汇报并保留 todos/checkpoint，回复“继续”接着做；
   普通问答维持原 6 次预算不受影响。
2. **CLI 优化**：全局命令 `myragagent`（安装 `scripts\install-myragagent.ps1`，
   任意终端直接输入，类似 `claude`）；新增 `/todos /status` 命令；
   流式文本按终端宽度换行；工具耗时、计划进度、任务清单渲染；数字键审批。
3. 测试增至 55 个（新增 task_mode 识别与预算单测）。

### 4.10 本轮（按目录 AGENTS.md + /resume + / 命令菜单）
1. CLI 传递 `project_dir`，后端按启动目录加载 AGENTS.md（类 CLAUDE.md 目录语义）；
   `/init` 在当前目录创建模板；
2. 会话 id 按目录持久化到 `.myragagent_session.json`，`/resume` 恢复、`/new` 清空；
3. 输入 `/` 实时列出可用命令，Tab 自动补全（Windows msvcrt 实现）。

### 4.11 本轮（记忆层级合并 + /resume 会话选择）
1. `load_project_memory` 按 CLAUDE.md 语义：启动目录逐级向上合并父目录
   AGENTS.md + 用户级 `~/.myragagent/AGENTS.md`，总长上限 8000 字符；
2. `/resume` 列出本目录最近 10 轮会话（含标题）供选择，支持 `/resume 序号`；
3. 新增分层记忆单测（57 通过）。

### 4.12 本轮（CLI Ctrl+C 打断修复 + 类 Claude Code 终端体验）
1. **根因**：CLI 主线程阻塞在 `urllib` 的 socket 读上，Windows 下 Ctrl+C 只能在
   读返回后的字节码边界生效，长工具执行期间几乎无响应；且断开后后端要等下一次
   向已关闭连接 yield 才感知。
2. **后端取消机制**：`app/api/agent.py` 新增运行注册表
   （`register_active_run` / `request_cancel` / `unregister_active_run`）+
   `POST /api/agent/cancel/{run_id}`；`session` 事件携带 `run_id`。取消立即置
   `stop_event`，agent 在 LLM 分片/工具循环检查后收尾，`done` 带 `stopped=True`。
3. **CLI 重写**（仍纯标准库，参考 Claude Code 交互设计）：
   - 键盘读取线程与 SSE 读取线程分离，主线程轮询队列 → Ctrl+C/Esc/Ctrl+D 在生成期间
     即时打断，并调用取消接口；
   - 流式 Markdown 渲染（标题/列表/引用/代码块/行内代码/粗体/链接/表格），
     CJK 宽字符正确换行，控制字符过滤防转义注入；
   - 工具调用卡片（`⏺ name(args)` → `⎿ 结果(耗时)`）、执行中 spinner+已耗时、
     计划进度条；
   - 权限审批改为带边框弹窗（1/2/3/4，回车=批准，Esc=拒绝）；
   - 状态栏：会话/模式/耗时/tokens（读 `/api/runs`）；新增 `/cost`、`/clear`、
     `/memory`（查看 AGENTS.md 加载链）；
   - ↑/↓ 历史（`.myragagent_history.json`）、Ctrl+L 清屏、Ctrl+R 重发、Tab 补全
     （含 /tools 参数）、打字先行（生成期间输入留到下一轮）；
   - 打断后保留已输出内容，提示“按 ↑ 找回问题”。
4. **测试**：新增 `tests/test_cli_agent.py`（18 个纯函数单测）与
   `tests/test_api_agent_cancel.py`（run_id 注入 + 取消停流的 anyio 逐帧集成测试），
   全量 **78 通过**。
5. **真机验证**：重启后端后实测——session 带 run_id，取消返回 `cancelled:true`，
   `done` 在 2.7s 内到达且 `stopped=True`。

### 4.13 本轮（CLI 输入渲染修复 + 后端网络权限坑）
1. **输入显示堆叠修复**：长输入超过终端一行时，`\r\033[2K` 只清最后一行，
   上一行残影每次按键叠加成多行 `› ...`。改为按行布局
   （新增纯函数 `input_layout`，CJK 宽字符正确计算行/列），重绘时回清上次
   占用的全部行、清残留行、光标跨行定位；回车前重绘去掉幽灵补全，
   非终端（管道）输入不回绘防转义污染。
2. **“Connection error.” 根因**：上一轮从沙箱里重启后端，新进程继承了沙箱
   网络限制，出站连 DeepSeek 报 `httpx.ConnectError: [WinError 10013]`。
   已脱离沙箱（提权）重启恢复，实测回答 82 输出 tokens、prompt 3597 正常。
   CLI 对 Connection error 增加“检查后端日志/网络”提示。
3. 测试增至 **79 通过**（新增 `input_layout` 布局单测）。

### 4.14 本轮（run_hooks 漏导入致命修复 + CLI Ctrl+C 加固）
1. **致命 bug：`_tools_node` 调用 `run_hooks` 但从未导入**。`from ..hooks
   import run_hooks` 只存在于 `_subagent_node`，主 agent 路径每次调工具都
   `NameError`（CLI 报 `✖ name 'run_hooks' is not defined`，正常对话直接断）。
   修复：`_tools_node` 补上同款局部导入；新增回归单测
   （检查 `_tools_node` 源码含该导入，防止再次漏掉）。
2. **CLI Ctrl+C 加固（Claude Code 风格）**：
   - 提示符下 Ctrl+C（Windows 信号路径 KeyboardInterrupt）此前从
     `reader.get()` 逃逸，直接打到 `main()` 外层 finally 退出整个 CLI；
     现在 `read_line` 内捕获转成 `Interrupted`，主循环语义对齐 Claude Code：
     输入框有内容时 Ctrl+C/Esc 清空输入留在 CLI；**空提示符下第一次 Ctrl+C
     提示“再按一次退出”，1.5s 内再按一次退出回终端命令行**（双击退出），
     退出也可走 Ctrl+D 或 /exit /quit；
   - 打断生成的清理期间（`_cancel_run` 最多 3s）再按 Ctrl+C 会二次逃逸退出，
     现在捕获视为“已打断”处理；
   - `_run_windows` 键盘线程捕获 KeyboardInterrupt 转成 ctrl-c 事件塞回队列，
     避免键盘线程被信号打死、之后输入全部失灵。
3. 测试增至 **82 通过**（新增 2 个 read_line Ctrl+C 单测 + 1 个 run_hooks 回归）。

### 4.15 本轮（同类漏导入清剿 + 工具并行读 DB 的 Session 并发修复）
1. **同类 NameError 清剿**：实测发现 `_run_sensitive_subagent_tool`（子代理
   敏感工具 HITL 路径）调用 `describe_tool_call`/`display_args` 但未导入，
   子代理写文件/命令时同样 `NameError`。修复：补上局部导入。
   用 pyflakes 全文件扫描确认 **0 个 undefined name**。
2. **工具并行读 DB 的 Session 并发修复**（复现：多工具并行时后端日志
   `Packet sequence number wrong` → 请求级 Session 事务损坏 → 后续所有
   会话/消息/运行记录写入全部失败）：
   - `hooks.py::load_hooks` 与 `todos.py::load_todos` 改为**优先用独立短会话
     （SessionLocal）读取**，与 §4.1 todo_update 的独立会话方案一致；
     工具线程池并行执行时不再共用请求级 Session；
   - 单线程路径（prepare/finalize 主线程）自动回退请求级 Session，行为不变；
   - 实测：子代理创建目录+写文件+读回确认全链路 33s 完成，日志零 WARNING。
3. 测试仍 **82 通过**（新增并发读取不影响现有单测，repo.get_meta 被
   monkeypatch 时回退传入 db 参数，测试兼容）。

### 4.16 本轮（CLI 体验：会话工作目录 + 审批即时生效 + todo 数字序号）
1. **会话工作目录跟随 CLI 启动目录**（用户反馈：新目录启动 myragagent 时
   bash 沙箱目录与文件系统不一致）：
   - `tools_extra._resolve_workspace` 新增 `project_dir` 参数；所有
     `make_*_tool` / `make_agent_tools` / `_run_command` / `_subagent_tools`
     / `_dispatch_tasks` 全链路传递；
   - **conversations 表新增 `project_dir` 列**（已 ALTER TABLE 落地）：
     新会话建号时保存启动目录，`/resume` 恢复时读取会话保存的目录，
     从任意目录恢复会话工作目录都一致；
   - 优先级：`tool_workspace` 显式配置 > 会话 project_dir > 项目根。
2. **审批数字键/回车即时生效**（用户反馈：按数字后还要再按回车）：
   `prompt_permission` 去掉按数字后的 `read_line` reason 输入环节，
   数字键/回车/Esc 按下立即 resolve 并继续执行。
3. **todo_update 数字序号匹配**（run 160 里模型用 `task_id="1"` 连续报
   "标记失败：任务不存在"）：`complete/remove` 支持纯数字 task_id 按
   清单顺序匹配第 N 项（1-based），兼容模型把"步骤 1"写成数字的常见情况。
4. 测试 **82 通过**；提交 `90b5417`。

### 4.17 沙箱策略切换：默认 subprocess（Claude Code/Codex 同款）
- **决策**：用户明确要求日常开发用 Claude/Codex 同款策略——bash 直接在
  宿主环境执行（`subprocess`），继承 `D:\conda_envs\pytorch_env` 全部依赖；
  docker 隔离沙箱保留为可选（设置 `command_sandbox: docker` 一键切回）。
- 实现：无需改代码（`config.py` 默认本就是 `subprocess`）——之前是设置页
  把 `runtime_overrides` 覆盖成了 `docker` + 只读挂载。通过 API 重置：
  `PUT /api/settings {"updates":{"command_sandbox":"subprocess","sandbox_workspace_readonly":false}}`。
- 验证：subprocess 下 `python` 解析到 `D:\conda_envs\pytorch_env\python.exe`，
  `import langchain_core/pymysql/sqlalchemy` 全部可用，cwd 为项目根。
- 遗留：`command_allowlist=python, dir, echo` 是"自动放行前缀"，未命中命令
  仍走 HITL 审批；`--network=none` 等 docker 隔离参数只在 docker 模式生效。

### 4.18 CLI 行编辑换 prompt_toolkit（修输入显示/光标/Ctrl+C）
- **背景**：用户反馈 CLI 输入三个问题——最后一个字符看不到、光标偶尔
  移动不了/无法回车、Ctrl+C 结束不了。根因：手写 KeyReader 线程 + 手写
  ANSI 光标控制（`_render_input`/`input_layout`），Windows 终端上宽度计算、
  光标恢复、信号与读键线程竞争均有 bug。
- **方案**：`read_line` 换 **prompt_toolkit**（Hermes/Claude Code 同款方案），
  光标、宽字符、跨行、历史、Tab 补全全部由它接管：
  - `make_prompt_session()`：PromptSession + InMemoryHistory + 补全适配
    （复用 `complete()` 的 `/` 命令与 `/tools` 候选）+ 键绑定
    （Esc 清空 / Ctrl+D 空输入退出 / Ctrl+R 重发）；
  - Ctrl+C：空输入 → `Interrupted(False)`（主循环双击退出逻辑不变），
    有输入 → prompt_toolkit 默认清空继续，语义与旧实现一致；
  - **非 TTY 回退**：`sys.stdin.isatty()==False`（管道/IDE/测试）走
    `_read_line_legacy`，其中非 TTY 直接 `readline()`（msvcrt 在管道下
    kbhit 恒 False 会死等，旧实现有此隐患）；
  - git-bash（xterm）下 prompt_toolkit 抛 NoConsoleScreenBufferError，
    try/except 自动回退 legacy；cmd.exe/myragagent 走完整 prompt_toolkit。
- KeyReader 保留：SSE 流式期间的审批弹窗（`prompt_permission`）与
  Ctrl+C 打断仍用它（单键读取，不需要行编辑）。
- 测试：82 通过（2 个 Ctrl+C 测试改为强制 legacy 路径 + mock
  KeyReader.get）；requirements.txt 加 `prompt_toolkit>=3.0.43`。
- 提交 `896066b`。

1. **HITL 人工确认**落地：`permissions.py` + tools 节点审批门 + 审批 API + 前端审批卡 + CLI 审批。
2. **工具集升级**：`tools_extra.py` 重写为 `list_dir/read_file/grep_search/write_file/edit_file/delete_file/bash`；原子写入；删除进 `.agent_trash/`；`edit_file` 返回 `diff.before/after` 供可视化审批。
3. **修复 run#55**：write_file 绝对路径失败烧光预算导致任务半途而废 → 失败不烧预算 + 重试引导 + 路径容错。
4. **记住命令**：永久白名单 + 本会话记住。
5. **自动规划取代手动计划模式**（用户反馈后改）：去掉前端/CLI 的计划模式开关，规划由 agent 自动判断；自动规划覆盖文件/命令任务；任务清单与计划进度联动打对钩。
6. **渲染修复**：流式结束正文消失（watch plain 强制渲染）；流式/完成字体对齐防闪烁；滚动节流 + `overflow-anchor:none`。
7. **审批卡折叠**：处理后变为一行紧凑状态条。
8. **TodoWrite 任务清单**：`todos.py` + `todo_update` 工具 + API + 前端勾选 + 会话恢复。
9. **bash 工作目录一致性**：bash 默认在工作目录根执行（与文件工具一致）。
10. **README 更新**：文件/命令执行、HITL、记忆、环境变量、API 一览等章节。

### 4.19 本轮（2026-08-16：P0 交互对齐 Claude Code + CLI 美化 + 预热修复 + P1 三项 + P2 与知识库管理）

**P0 交互四件套**（提交 `5dfdafe`）：
1. **diff 审批渲染**：`permissions.py` 新增 `build_diff_lines()`（difflib 行级 diff），
   `display_args` 的 edit_file 预计算 `diff_lines`（预览放宽到 800 字符）；
   CLI 审批弹窗逐行红删绿增，write_file 显示内容预览；Web 审批卡行级 diff。
2. **工具输出展开**：`_tool_detail()` 随 `tool_result` SSE 事件与 tool_trace 持久化
   `detail`（3500 字符截断，不影响模型历史瘦身）；CLI `Ctrl+O` / `/output [n]`
   展开最近工具输出；Web 工具卡点击展开。
3. **消息级回退**：`POST /conversations/{id}/rewind`（删除该消息及之后 +
   重置过期摘要 + 清 todos/checkpoint）；CLI `/rewind` 选消息回退并预填输入框；
   Web 用户消息悬停「回退」按钮。
4. **/compact 与 /context**：`GET /agent/context/{id}`（占用统计）+
   `POST /agent/compact/{id}`（强制压缩，保留 max(4, N/4) 条近期）；
   CLI 双进度条命令；Web 头部「上下文」面板（占用条 + 一键压缩）。

**CLI 界面美化**：输入区状态行（全宽分隔线 + 模型/工具/会话/沙箱/上下文 meter）、
底部常驻快捷键工具栏（prompt_toolkit `bottom_toolbar`）、空输入 `?` 展开/收起
完整命令面板、盒式横幅 + 启动命令总览、工具卡配色。

**性能修复**（提交 `ddea54b`）：排查 run207-209「慢+缓存差」——56s 检索是
热重载后 BGE 模型冷启动（会话中编辑 backend 文件触发 reload 所致）。
修复：lifespan 后台预热 embedding/reranker；`run.py` `reload_excludes`
排除 cli_agent.py/日志（客户端编辑不再误杀服务端）。

**P1 三项**（提交 `c231dd0`）：
1. **分阶段耗时打点**：`_timed_node` 包装七个图节点（多轮/多分支累加）+
   TTFT（首个内容 chunk）；落 `agent_runs.token_usage.timings`；Web 运行记录
   详情显示每阶段占比条。注意修过 `state.get("runtime") or {}` 空字典 falsy 坑。
2. **结构化输出**：`context.py` 新增 5 个 Pydantic schema，plan/事实提取/记忆整合
   优先 `with_structured_output`（工具调用式），失败回退旧正则解析。
3. **沙箱一键切换**：请求级 `command_sandbox`（schema 校验 subprocess|docker）
   → run() → runtime → 主循环与子代理 bash 工具覆盖设置页；
   CLI `--sandbox` 启动参数 + `/sandbox docker|subprocess|off`。

**P2 + 知识库管理**（提交 `c8a6e78`）：
1. **文档预览**：`GET /documents/preview`（md 原文/其余提取文本，20 万字符封顶）；
   KnowledgeView 抽屉渲染（md 用 MarkdownContent）。
2. **自定义分类**：新表 `document_meta`（relative_path 唯一：category/tags/notes，
   create_all 自动建表）；`GET|PUT /documents/meta`；文档列表合并分类列，
   下拉筛选 + 编辑对话框（分类可新建）。
3. **用量仪表盘**：`GET /runs/stats?days=N`（tokens/命中率/估算成本/每日趋势，
   成本按 deepseek 量级单价估算）；RunsView 顶部统计卡 + 趋势条。
4. **CLI headless**：`cli_agent.py -p "问题" --output-format text|json`
   （类 claude -p，json 含 answer/sources/tool_trace/token_usage/timings）。
5. 长任务（>10s）完成终端 bell；`scripts/regression_gate.py` RAGAS 回归门槛
   （不进 pre-commit，改动检索/提示词后手动跑）。

**架构决策（与用户讨论定稿）**：多用户**不做**（单用户本地，单例状态/本地模型
都是单用户假设；将来要共用走多配置文件而非租户体系）；**Java 后端不需要**
（核心资产全在 Python 生态，瓶颈在 LLM/本地模型不在框架）。

**下一步已定**：接专业开源文档查看器（pdf.js/epub.js/docx-preview + 文本类
content-velocity 方案）+ 引用「查看原文」定位 + 位置记忆——完整方案见
**`docs/DOCUMENT_VIEWER_PLAN.md`**（新会话直接按它开工）。

### 本会话遗留的小事
- 桌面 `C:\Users\user\Desktop\practice2` 是测试产物（内容已清空），删除被 Windows 拒绝（疑似占用/权限），**用户手动删除即可**。
- `docs/` 下还有 `AGENT_COMPARISON.md`（与主流 agent 对比）、`HERMES_STYLE_AGENT.md`（终端/ACP 路线），写文档前先读，避免重复。



---

## 5. 已知待办与下一步（用户未定方向，先讨论再动手）

> **已落地（2026-08-12 第二/三轮）**：git 版本保护（`rag_knowledge_base/`）
> + pytest 25 用例 + CUDA 容错（自动降级 CPU / 冷却恢复）
> + 任务清单跨轮修复 + 系统提示词注入修复 + 模板重写
> + Skills 自动注入与防注入清洗。仍缺：评测体系（RAGAS 类指标与回归门槛）、
> CI、LangGraph 原生 checkpointer 迁移、成本优化（记忆提取条件化 / 前缀缓存）。

### 用户明确提过的方向
- **P0/P1 已基本完成**（MCP、受控执行、checkpoint、轨迹压缩、记忆整合、SSE 心跳、空参数兜底、标题后置等），文档里的 P0/P1 计划已实现；**用户隔离暂不做**。
- 下一步候选（前几轮讨论过）：
  1. ~~模型主动维护 todo 并把计划变成硬约束~~ **已落地（2026-08-12，见 §4.1）**。
  2. **子代理/并行分支**（Claude Code 式：搜索代码 + 读文档 + 验证并行）。
  3. **TUI 终端客户端**（Textual，后端零改动）或 **ACP 桥**（让 claude/codex 客户端驱动本 agent）。
  4. **hooks/脚本扩展**（PreToolUse/PostToolUse 式门禁）。
  5. **审批成本预估**（diff 行数 + token 成本）。
  6. **PWA/Tauri 打包**。

### 架构/设计讨论结论（用户认可的）
- 底层是 **LangGraph**（不是裸 LCEL 手工循环）；整个项目用 **LangChain 框架**。
- 用户在学习 agent 技术，目标类似 Hermes/Claude Code：终端可对话、读写文件、执行命令、写代码。
- 用户偏好：工程化、可观测、本地优先；“先做能演示的”；中文回答；遇到问题先查根因再动手。

---

## 6. 常用入口速查

### API（前缀 `/api`）
- `POST /agent/stream`：SSE 流式 agent（最常用）。
- `POST /agent/chat`：非流式。
- `POST /agent/permission/{id}/resolve`：审批（approve/reason/remember/remember_session）。
- `GET /agent/permissions`：待审批列表。
- `GET/PUT /todos/{conversation_id}`：任务清单。
- `GET /conversations`、`GET /conversations/{id}/messages`：会话与消息。
- `GET/POST/PATCH/DELETE /memories`：长期记忆。
- `GET/PUT /settings`：运行时配置。
- `GET /documents`、`POST /documents/upload`、`POST /index/rebuild`：知识库。
- `GET /runs`、`GET /runs/{id}/trace`：运行记录。
- `POST /tools/execute`：设置页测试受控工具。
- `GET/PUT /mcp`、`POST /mcp/test`：MCP 服务器管理。
- 自动文档：`http://127.0.0.1:8000/docs`。

### 数据库表（`rag_assistant`）
`conversations` / `messages` / `templates` / `memories` / `agent_runs` / `app_meta`（runtime_overrides、todos_*、mcp_servers、suggestions_* 等键值）。

### 常用日志
- 后端：`backend/uvicorn.out.log` / `uvicorn.err.log`。
- 运行 trace：`opensearch_meta/traces/YYYY-MM-DD.jsonl`。

---

## 7. 给新会话的三条建议

1. **先读 `README.md` + 本文件**，再看 `docs/AGENT_COMPARISON.md`（对比）和 `docs/HERMES_STYLE_AGENT.md`（路线），最后按需读 `langgraph_agent.py` 和 `permissions.py`。
2. **改动前先确认运行中的后端 PID**（`netstat -ano | findstr :8000`），改完后端必须带环境变量重启；**重启务必脱离沙箱/提权**，否则新进程继承沙箱网络限制，连模型 API 会报 `WinError 10013`。
3. **前端改完跑 `npm run build` 验证**；vite dev server 若仍在运行，HMR 会自动生效。
