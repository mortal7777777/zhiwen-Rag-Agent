# FastAPI 学习指南：从请求到响应，用本项目源码讲透

> 适用对象：会 Python 基础，想通过**本项目后端**达到"熟练 FastAPI"水平。
> 学习方式：每个概念 = ① 本质一句话 → ② 源码对照（路径:行号）→ ③ 动手实验。
> 涉及前后端通信的部分请配合 [DATA_FLOW_GUIDE.md](DATA_FLOW_GUIDE.md)；Vue 侧见
> [VUE_GUIDE.md](VUE_GUIDE.md)。全篇路径默认相对 `backend/`。

---

## 0. 后端地图：一个请求会路过哪些文件

```
backend/
├── run.py                     # 便捷启动脚本（加载 .env.local）
├── requirements.txt
├── tests/                     # pytest 测试套件（test_api_models.py 等）
└── app/
    ├── main.py                # ★ FastAPI 应用实例：注册中间件/路由/lifespan
    ├── config.py              # Settings：全部可配置项（env 可覆盖）
    ├── schemas.py             # ★ Pydantic 模型：请求/响应契约（自动校验+文档）
    ├── api/                   # ★ 路由层（薄）：参数校验、调用 service、错误映射
    │   ├── deps.py            # 依赖注入：全局单例 service
    │   ├── chat.py            # POST /api/chat（RAG 问答 JSON）与 /chat/stream（SSE）
    │   ├── agent.py           # POST /api/agent/stream（Agent 流式，最复杂）
    │   ├── documents.py       # 知识库文件 CRUD + 上传（UploadFile）
    │   ├── settings.py        # 运行时设置读写（GET/PUT /api/settings）
    │   ├── conversations.py / memories.py / templates.py / runs.py
    │   ├── health.py / skills.py / suggestions.py / vision.py / advanced.py
    ├── db/
    │   ├── database.py        # SQLAlchemy engine/session + get_db 依赖
    │   ├── models.py          # ORM 表模型（conversations/messages/…）
    │   └── repository.py      # 数据访问函数（repo.*）
    ├── rag/                   # RAG 业务层：service.py 是核心门面
    │   ├── service.py         # RAGService：ask/retrieve/文档管理
    │   ├── retriever.py       # 混合检索（BM25+kNN）
    │   ├── query_expander.py  # Multi-Query/HyDE 查询扩展
    │   ├── reranker.py / embeddings.py / llm.py / splitter.py / store.py / loader.py
    ├── agent/                 # Agent 编排层（LangGraph 图）
    └── runtime_config.py      # 设置覆盖：MySQL 里的值压过 config 默认值
```

一句话架构：**api 层薄、业务层厚**——路由文件只做"收参数、调 service、把异常变成
HTTP 错误"，所有逻辑在 `RAGService` / `AgentService` 里。读代码时先分辨
"这层负责什么"，这是 FastAPI 项目最重要的分层意识。

启动与自测：

```bash
cd backend
python -m uvicorn app.main:app --reload --port 8000
# 打开 http://127.0.0.1:8000/docs   ← FastAPI 自动生成的交互式接口文档
# 每个接口都能在 Swagger UI 里直接点 "Try it out" 发请求
```

---

## 1. 入口 main.py：应用实例的组装

`app/main.py` 只做四件事：

**① 创建应用实例**（:99-107）：

```python
app = FastAPI(
    title=settings.app_name,
    description="个人知识库 + 智能 Agent：…",
    version=settings.version,
    lifespan=lifespan,
)
```

**② 注册 CORS 中间件**（:110-116）：

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

概念：CORS 是**浏览器**的同源策略（不是后端安全机制）。前后端不同端口时浏览器会拦截
跨域响应，CORS 头告诉浏览器"这个域可以读"。本项目开发期有 Vite 代理（同源，
见 DATA_FLOW），CORS 配置是给直连场景（如 eval_ragas.py 脚本直调 8000 端口）兜底的。

**③ 注册路由**（:118-130）：13 个 router 全部 `include_router(xxx_router, prefix="/api")`。
这样每个 router 文件里写 `@router.get("/documents")`，最终对外就是 `/api/documents`。

**④ lifespan 启动钩子**（:43-96）：FastAPI 的 `lifespan` 是一个
`asynccontextmanager`，`yield` 之前是启动逻辑、之后是关闭逻辑。本项目启动时：
目录准备 → `init_db()` 连 MySQL 建表 → 从 MySQL `load_overrides()` 读用户设置 →
起一个 daemon 线程 `_warmup_rag_models()` **后台预热本地 BGE/Reranker 模型**
（不预热时重启后首次检索要付 30-60 秒模型加载——代码注释里记录了 run#208 56s 检索的教训）。

> 概念小结：**main.py 只组装不写业务**。看到新项目先读它的 main.py，10 分钟内
> 就知道有哪些接口、挂在什么前缀下、启动做了什么。

## 2. 路由层：一个接口长什么样

最完整的"最小示例"在 `api/health.py`（新项目先抄它）。以 `api/documents.py`
的一个接口为例拆解（:43-58）：

```python
@router.get("/documents", response_model=list[DocumentInfo])
def list_documents(
    service: RAGService = Depends(get_service),   # 依赖注入（§4）
    db=Depends(get_db),
) -> list[dict]:
    docs = service.list_documents()
    try:
        metas = repo.list_document_meta(db)
    except Exception:
        metas = {}
    for doc in docs:
        meta = metas.get(doc.get("relative_path") or "") or {}
        doc["category"] = meta.get("category", "")
        doc["tags"] = meta.get("tags", "")
    return docs
```

FastAPI 的魔法：**函数的参数声明 = 请求契约**——

| 参数写法 | 从哪里取值 | 项目实例 |
|---|---|---|
| `path: str`（路径里有 `{id}`） | URL 路径 | `/documents/{relative_path:path}` |
| `path: str = Query(...)` | URL 查询串 `?path=xx` | documents.py:98 预览接口 |
| `request: ChatRequest`（Pydantic 类型） | 请求体 JSON | chat.py:20 |
| `files: list[UploadFile] = File(...)` | multipart 表单文件 | documents.py:187 上传 |
| `service: RAGService = Depends(get_service)` | 依赖注入容器 | 所有接口 |
| `db = Depends(get_db)` | 依赖注入（会话） | 用 MySQL 的接口 |

**路径参数的特殊写法**：`@router.delete("/documents/{relative_path:path}")`
（documents.py:199）里的 `:path` 是 FastAPI 的路径转换器——默认 `{x}` 不匹配斜杠，
`:path` 允许 `a/b/c.pdf` 这种含目录的路径（文档用相对路径作 id 的原因）。

**返回什么**：路由函数返回普通 Python dict / list，FastAPI 自动转 JSON；声明了
`response_model` 时还会按模型过滤字段并校验类型（多传的字段会被剔除——想返回
"计算出来的额外字段"但没在模型里就会报错，这是新手常见困惑）。

## 3. Pydantic：请求体的自动校验

`schemas.py` 是**前后端契约**。看两个典型的（:10-22）：

```python
class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$", description="user 或 assistant")
    content: str

class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="用户问题")
    history: list[ChatMessage] = Field(default_factory=list, description="历史对话")
```

要点：

- `...` 表示必填；`default_factory=list` 表示不传时给空列表（**不要写 `default=[]`**，
  mutable 默认值是 Python 经典坑，Pydantic 会自动帮你规避但仍应养成习惯）；
- `min_length/max_length/pattern/ge/le` 等约束不满足时，FastAPI 直接返回 **422**
  加详细错误（不需要你写 if 校验）——在 /docs 里故意发个空 question 看返回形状；
- 请求体会**递归校验**：`history` 里每一项都必须是合法 ChatMessage；
- Agent 的请求模型 `AgentChatRequest`（schemas.py:74-128）是最大的契约：9 个可选字段
  控制工具模式/图片/计划模式等，每个都有 description——**Swagger 文档自动生成自这里**。

## 4. 依赖注入：Depends 到底干了什么

FastAPI 的 DI 是它的招牌：`Depends(函数)` 表示"执行本接口前先执行这个函数，返回值
作为参数传进来"。链式依赖、单例、请求级生命周期都是这么表达的。

`api/deps.py`（全文 21 行）定义了两个**进程级单例**：

```python
@lru_cache
def get_service() -> RAGService:
    return RAGService(get_settings())          # 全进程只构造一次

@lru_cache
def get_agent_service() -> LangGraphAgentService:
    return LangGraphAgentService(get_settings(), get_service())
```

`@lru_cache` 保证：无论多少请求进来，`get_service()` 只执行一次，之后全进程共享同一个
RAGService（它内部持有 embedding/reranker/索引等重资源——**绝对不能每请求新建**）。
注意它已经通过 `get_service()` 做了依赖套依赖，get_agent_service 内部包了一个 RAGService。

`db/database.py` 里的 `get_db()`（:170-181）是另一种形态——**yield 依赖**：

```python
def get_db():
    if not db_ready or SessionLocal is None:
        raise HTTPException(status_code=503, detail="数据库未连接：…")
    db = SessionLocal()
    try:
        yield db          # 接口执行期间可用
    finally:
        db.close()        # 接口结束（含异常）必关
```

概念：**lru_cache 依赖 = 单例（整个进程一份）；yield 依赖 = 每请求一个、用后即焚**。
`get_db` 用 yield + finally 实现了"每请求一个 SQLAlchemy 会话，无论成功失败都归还连接"，
这是数据库会话的正确姿势。

## 5. 错误处理：HTTPException 与异常映射

FastAPI 惯例：业务代码抛异常，路由层捕获后翻译成 HTTP 状态码。看 documents.py 的上传
接口（:185-196）：

```python
@router.post("/documents/upload", response_model=list[DocumentInfo])
def upload_documents(
    files: list[UploadFile] = File(...),
    service: RAGService = Depends(get_service),
) -> list[dict]:
    try:
        return service.add_documents(files)
    except ValueError as exc:                       # 业务校验失败 → 400
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:                        # 未知错误 → 500
        raise HTTPException(status_code=500, detail=f"上传失败：{exc}")
```

代码注释里还交代了为什么把详细异常塞进 detail：开发期方便调试。但**生产环境不要**
把内部异常直接回给客户端（泄露路径/库版本），应记日志返回通用文案——本项目是本地
工具所以从简。错误响应形状统一是 `{"detail": "…"}`——前端 axios 用
`error.response?.data?.detail` 取文案（ChatView.vue:1860 就是这种取法）。

## 6. 同步接口与异步接口：FastAPI 的线程模型

这是 FastAPI 进阶最重要的一课，本项目两个文件刚好是正反教材。

**规则**：路由函数是 `def`（同步）时，FastAPI 把它丢进**线程池**执行（不阻塞事件循环，
但并发受线程池限制）；是 `async def` 时直接在事件循环里跑（高并发友好，但函数内
**绝不能有阻塞调用**——会卡死整个服务）。

`api/chat.py:19` 的 RAG 问答是同步 `def`：

```python
@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, service: RAGService = Depends(get_service)) -> ChatResponse:
    service.acquire()          # 信号量：控制并发（GPU 保护）
    try:
        result = service.ask(...)      # 内部是同步的重活（向量检索+模型生成）
        return ChatResponse(**result)
    ...
```

`service.acquire()` 是 `threading.BoundedSemaphore`（rag/service.py:53-55）——
同一时刻最多 N 个请求在跑 GPU/检索（默认 4），这就是**应用层限流**。注意信号量在
同步线程池模型下成立；如果接口改成 async def 直接调同步 `service.ask` 就会阻塞事件
循环——这是本项目 API 层设计最容易被忽略的一个约束。

`api/chat.py:43` 的 `/chat/stream` 则是"必须用 async 时怎么处理同步重活"的正确示范
（SSE 流式必须 async 生成器，但检索是同步重活，所以每一步都显式丢线程池）：

```python
docs = await run_in_threadpool(service.retrieve, request.question, history)  # 重活离线
...
async for token in service.chat.astream(...):   # LLM 流式本身是 async 的，可以直接用
```

## 7. 流式响应 SSE：agent_stream 全拆解（必读）

`api/agent.py:86-165` 是项目最复杂的端点，也是理解"后端如何边干活边给前端报进度"的
范本。它解决的问题：Agent 一次运行可能长达几十秒（检索→思考→调工具→审批→生成），
前端需要**实时看到每一步**。

事件流格式约定（自定义 SSE，每个事件一行 JSON）：

```python
def _sse(payload: dict) -> str:                       # :55
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
# 产物: data: {"event": "tool_start", "data": {...}}\n\n
```

架构是经典**生产者-消费者**：worker 线程跑同步业务，把事件塞进 `queue.Queue`；
async 生成器从队列取事件 yield 给 HTTP 流。

```python
queue: Queue = Queue()
stop_event = threading.Event()
run_id = register_active_run(stop_event)              # 全局登记，供取消接口使用

def worker():                                          # ① 生产者：后台线程
    try:
        for event in service.run(...):                 # 同步生成器：边跑边吐事件
            queue.put(event)
    except Exception as exc:
        queue.put({"event": "error", "data": {"message": str(exc)}})
    finally:
        queue.put(None)                                # 结束哨兵

threading.Thread(target=worker, daemon=True).start()   # 线程立即启动，接口立刻返回流

async def _get_event():                                # ② 消费者：带超时取事件
    try:
        return await asyncio.to_thread(queue.get, timeout=HEARTBEAT_INTERVAL)
    except Exception:
        return "__heartbeat__"

while True:
    event = await asyncio.wait_for(_get_event(), timeout=HEARTBEAT_INTERVAL + 5)
    if event == "__heartbeat__":
        yield ": keepalive\n\n"                        # 心跳：SSE 注释行，防代理/浏览器断连
        continue
    if event is None:
        break                                          # worker 结束 → 关闭流
    if event.get("event") == "session":
        event = {**event, "data": {**(event.get("data") or {}), "run_id": run_id}}
    yield _sse(event)
```

三个必懂设计：

1. **为什么用线程 + queue 而不是 async 生成器里直接 await service.run？**
   `service.run` 是同步代码（内部调 OpenAI SDK 同步方法、本地模型），把它放进事件循环
   会阻塞心跳和响应；放线程里跑，事件循环只做"取队列→yield"，随时能响应其他请求。
2. **心跳**：工具执行可能 10-20 秒没有任何事件，HTTP 流干等会被代理/浏览器判定超时
   断开。每 15 秒 yield 一行 `: keepalive\n\n`（SSE 注释行，前端解析时自然跳过）。
3. **客户端断开**：浏览器关页面/点停止 → fetch 流被中断 → async 生成器抛
   GeneratorExit → `finally` 里 `stop_event.set()` + `unregister_active_run(run_id)`，
   后台 worker 的下一次停止检查会自己收尾（不落半截回答）。这就是"断线即停止"的实现。
   主动取消是另一个独立端点 `POST /agent/cancel/{run_id}`（:168-172）。

返回的 StreamingResponse 头（:158-165）：`media_type="text/event-stream"` +
`Cache-Control: no-cache`（禁止缓冲）+ `X-Accel-Buffering: no`（关掉 Nginx 层缓冲，
否则事件会被攒批而不是逐条到达）。

## 8. 数据库：engine → session → repository

`db/database.py` 的结构（SQLAlchemy 2.0 + PyMySQL）：

- `init_db()`（:29-88）：启动时"尽力连接"，连不上**自动尝试建库**（解析 MYSQL_URL 后
  用无库名的 URL `CREATE DATABASE`），再失败就降级（`db_ready=False`，对话仍可用）。
  这是本地工具的容错设计：MySQL 挂了服务照常起，只是记忆/模板功能不可用。
- `_activate_engine`：`Base.metadata.create_all(engine)` 建表 + `sessionmaker` 造会话工厂
  + 写种子数据（内置提示词模板）。
- `_migrate(engine)`（:111-167）：create_all 不会给旧表加列，所以对已存在的表做
  **增量 DDL**（查 information_schema → 缺哪列 ALTER 哪列）。没有用 Alembic，因为
  单机工具不值得引入迁移框架——这是"按项目规模选方案"的示例。
- `get_db()` 见 §4。

ORM 表（`db/models.py`）：Conversation / Message / DocumentMeta / AgentRun /
PromptTemplate / Memory / AppMeta。其中 **app_meta 存的是"运行时设置覆盖"**——
settings 接口写进去的值，`load_overrides()` 启动时读出来压过 config 默认值
（runtime_config.py 的 `effective()` 机制，见 DATA_FLOW 链路 D）。

分层习惯：路由不直接碰 ORM，而是调 `repository.py` 的 `repo.xxx(db, …)` 函数
（documents.py:51 `repo.list_document_meta(db)`）。想加表：models.py 加类 → repository.py
加读写函数 → 路由里 `Depends(get_db)` 调 repo。

## 9. 配置体系：Settings + 环境变量 + 运行时覆盖

- `config.py` 的 `Settings`：全部可配置项的 dataclass（数据目录、模型路径、各 URL、
  并发数…），每个字段配 `_env("KEY", 默认值)` 支持环境变量覆盖；
- 顶层调 `get_settings()` 拿到**进程级单例**；
- 用户改的设置（模型供应商、key、开关）存 MySQL `app_meta`，`effective()` 读值顺序：
  **MySQL 覆盖值 > 环境变量默认值**（runtime_config.py）——"设置页改完立即生效、
  重启不丢"就是这套机制；
- 路由里拿配置的姿势：`from ..config import get_settings` + `get_settings()`；
  service 在构造时把 settings 存成 `self.settings`。

## 10. 测试与调试

```bash
cd backend
python -m pytest tests/ -q          # 跑整个测试套件
python -m pytest tests/test_api_models.py -q   # 只看某一个
```

tests/ 里有 conftest.py + 按模块分的用例（API、agent、检索、去重、网络、文件…）。
测试里直接 import app 的 `TestClient` 打接口，或用真实 service 单测——先读
`tests/test_api_models.py` 学套路。

日常调试三板斧：
1. **Swagger**：http://127.0.0.1:8000/docs 手动发请求看 200/422 形状；
2. **日志**：main.py:34 配了 logging 格式，service 层有大量 logger.info（检索耗时、
   扩展条数），看后端终端；
3. **curl**：`curl -X POST http://127.0.0.1:8000/api/chat -H "Content-Type: application/json" -d '{"question":"你好"}'`

## 11. 动手：新增一个接口的完整清单

照着 `api/health.py` → `schemas.py` 的模式走一遍（目标是做一个
`GET /api/echo?text=你好` 返回 `{"text": "你好", "length": 2}`）：

1. `schemas.py` 加响应模型（需要时）；
2. 新建 `api/echo.py`（或塞进现有 router）：APIRouter + 一个函数；
3. `main.py` import 并 `include_router(echo_router, prefix="/api")`；
4. `uvicorn --reload` 会自动重启 → /docs 里试；
5. （可选）`tests/test_echo.py` 写 TestClient 用例；
6. 前端 `api/index.js` 加一行封装 → 页面调用（对照 VUE_GUIDE 练习 7）。

做完这个再尝试：带请求体的 POST（抄 chat.py）、依赖注入（Depends 抄 documents.py）、
把 `/documents/{relative_path:path}` 的路径参数换成 Query 体会差异。

## 12. 练习清单（按顺序，全部可本地验证）

1. 读一遍 `main.py`，不看书说出它做了哪四件事；
2. 在 /docs 里故意发非法请求（空 question、超长、错类型），把 422 返回体截图，
   找出 `detail` 数组里每个元素的含义（loc/msg/type）；
3. 给 `ChatRequest.question` 加一条 `strip_whitespace=True` 或改 max_length，看
   Swagger 文档与校验行为变化（改完还原）；
4. 自己实现 §11 的 echo 接口（10 分钟内完成算达标）；
5. 写一个返回 SSE 的接口：每秒 yield 一个计数（照抄 agent.py 的心跳+生成器骨架），
   用 curl -N 或浏览器 fetch 验证流式到达而不是攒批；
6. 给某个 GET 接口临时加 `async def` + 内部 `time.sleep(3)`，用两个并发请求观察
   服务卡顿——理解 §6 的线程模型（改完还原）；
7. 改 `get_db` 的 503 分支触发一次（临时把 db_ready 设 False），看响应形状，还原；
8. 在 repository 层写一个 repo 函数（如按标题模糊查会话），挂到 conversations 路由
   下用 /docs 验证；
9. 阅读 `tests/conftest.py`，给它测试用的 app/service 加一个你 echo 接口的依赖注入
   临时配置，写一条 pytest 用例；
10. 把 documents.py 的 `upload_documents` 异常分支补一个"文件为空"的 400 判断，
    并补测试——体会"路由只做错误翻译"的边界。

## 13. 常见坑清单（源码注释里挖出来的真实教训）

1. **async def 里写同步阻塞**：会把整个事件循环卡死（心跳/其他请求全停）。重活用
   `def`（FastAPI 自动线程池）或 `run_in_threadpool`/`asyncio.to_thread`。
2. **sync `def` 也要注意共享可变状态**：并发请求共享同一个 service 单例，信号量、
   锁要 thread-safe（本项目用 threading 原语，原因见
   [PYTHON_CONCURRENCY_GUIDE.md](PYTHON_CONCURRENCY_GUIDE.md)）。
3. **把内部异常直接 HTTPException(500, str(exc))**：本地工具可接受；对外服务要记日志
   回通用文案，防泄露。
4. **response_model 过滤多余字段**：返回 dict 里有模型外的键会 500（响应校验失败），
   不是静默丢弃——先在 /docs 看报错。
5. **mutable 默认值**：Pydantic 字段默认列表用 `Field(default_factory=list)`。
6. **lru_cache 单例里存会变的状态**：settings 值可能被运行时覆盖改掉，service 持有
   快照会过期——本项目用 `service.refresh()` 清缓存（settings.py:72-73 每次 PUT
   settings 后调用）重新读，想加"热更新配置"就抄这个模式。
7. **StreamingResponse 别忘关缓冲头**：`Cache-Control: no-cache` 与
   `X-Accel-Buffering: no`，否则事件攒批到达，流式变"一顿一顿"。
8. **路径参数与斜杠**：默认 `{x}` 不含 `/`，文档路径用 `{x:path}`。
9. **重载(reload)与日志文件**：`--reload` 监听目录变化，把输出重定向到 backend 目录
   下的文件可能触发 reload 循环杀请求（把日志文件放 backend 外或加 excludes）。
10. **升级包时先看约束注释**：requirements-dev.txt 里 "ragas 0.4 需要
    langchain-community<0.4" 这类注释是踩过坑才写的，别随手放开。

## 附：接口速查表（前端函数 ↔ 端点 ↔ 后端服务）

| 前端 api 函数 | HTTP 端点 | 后端路由文件 | 对应业务 |
|---|---|---|---|
| `chat()` | POST /api/chat | chat.py | RAG 问答（JSON，eval 脚本用） |
| `streamAgentChat()` | POST /api/agent/stream | agent.py | Agent 流式对话（主聊天） |
| `resolvePermission()` | POST /api/agent/permission/{id}/resolve | agent.py | HITL 审批 |
| `listDocuments()` | GET /api/documents | documents.py | 知识库列表 |
| `uploadDocuments()` | POST /api/documents/upload | documents.py | 上传建索引 |
| `previewDocument()` | GET /api/documents/preview | documents.py | 文档预览文本 |
| `getSettings()` / `saveSettings()` | GET/PUT /api/settings | settings.py | 运行时设置 |
| `listConversations()` | GET /api/conversations | conversations.py | 会话历史 |

完整的数据流转（含 SSE 帧协议、上传链路、设置生效链路）见
[DATA_FLOW_GUIDE.md](DATA_FLOW_GUIDE.md)。
