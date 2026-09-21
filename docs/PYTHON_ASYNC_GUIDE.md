# 协程与 asyncio 实战（结合 SSE 流式实现）

> 前置：先读 `PYTHON_CONCURRENCY_GUIDE.md`（概念总览）。
> 目标：看懂 FastAPI 的 async 端点、SSE 流式、`run_in_threadpool`，能解释"为什么流式必须用异步"。
> 代码对照：`app/api/chat.py`、`app/api/agent.py`、`app/rag/llm.py`。

---

## 1. 从生成器到协程：yield 的进化

基础篇讲过生成器（`yield` 暂停/继续）。协程就是"能暂停等待外部结果"的函数：

```python
# 普通函数：一条路走到底
def normal():
    a = get_data()     # 等 2 秒，期间啥也干不了
    b = get_data()     # 再等 2 秒
    return a + b       # 总耗时 4 秒

# 协程：每个 await 都是"暂停点"，等待时让出给别的协程
async def coroutine():
    a = await get_data_async()   # 等 2 秒，但等待期间事件循环去跑别的协程
    b = await get_data_async()   # 再等 2 秒，期间也在跑别的
    return a + b                 # 总耗时还是 4 秒，但别的请求没有被卡住
```

**核心区别**：普通函数阻塞时"整个线程卡住"；协程 `await` 时"只是这个协程暂停，事件循环去执行别的协程"。这就是"一个线程扛大量 IO 等待"的原理。

## 2. 事件循环（Event Loop）

```
事件循环 = 调度器。维护一个"就绪队列"，谁就绪跑谁：
  协程 A await 网络 → 挂起（注册"数据到了叫我"）
  协程 B 开始执行 → 也 await → 挂起
  ...（网络数据到达）→ 事件循环唤醒 A → A 继续
```

- 整个进程**只有一个事件循环线程**（FastAPI 启动 uvicorn 时自带）
- 所有 async 函数都在这个循环里"轮流跑"
- **纪律**：协程里绝不能做阻塞操作（同步 IO/CPU 计算），否则整个循环卡住 = 所有请求排队

## 3. async/await 语法速成

```python
import asyncio

async def fetch(name: str):          # async def = 协程函数
    await asyncio.sleep(1)           # await = 让出（等价真实场景的网络等待）
    return f"{name} 完成"

async def main():
    # 方式 1：顺序（总耗时 3 秒）
    a = await fetch("A"); b = await fetch("B"); c = await fetch("C")

    # 方式 2：并行（总耗时 1 秒）——IO 密集并行
    results = await asyncio.gather(fetch("A"), fetch("B"), fetch("C"))

    # 方式 3：后台任务（不等它）
    task = asyncio.create_task(fetch("后台"))
    ...做别的事...
    await task                       # 最后等它

asyncio.run(main())                  # 入口：启动事件循环
```

**和线程池的关系**：`asyncio.gather` 和 `ThreadPoolExecutor` 都是"并行等 IO"，区别：gather 在单线程里协作式切换（开销极小、能扛上万个），线程池是真开线程（有 GIL 和线程数上限）。**你的项目查询扩展用线程池**（因为 DeepSeek 客户端是同步库，且只有 3 个并发，线程池够用）；**SSE 流式用 asyncio**（长连接多、需要逐 token 推送）。

## 4. FastAPI 异步端点：为什么流式必须 async

FastAPI 端点可以写同步（def）或异步（async def）：
- **同步 def**：FastAPI 自动扔进线程池执行——简单，但每请求占一个线程
- **async def**：跑在事件循环里——等 IO 时让出，一个线程扛所有连接

SSE 流式为什么必须 async：一个流式请求可能挂 10 分钟（工具执行 + 逐 token 生成）。如果它是同步端点，这 10 分钟就占死一个线程；100 个并发流式请求 = 100 个线程（线程有栈内存、有切换成本，扛不住）。异步端点下这 100 个连接只是事件循环里的 100 个协程，内存开销极小。

**项目例子（api/chat.py:44-75）**——先看结构再逐句读：

```python
@router.post("/chat/stream")
async def chat_stream(request: ChatRequest, service=Depends(get_service)) -> StreamingResponse:
    """SSE 流式问答：先推 sources 事件，再逐 token 推回答。"""

    async def event_generator():                    # 异步生成器 = SSE 的核心
        history = [m.model_dump() for m in request.history]
        try:
            # ① 同步重活一律扔线程池——绝不阻塞事件循环
            await run_in_threadpool(service.acquire)          # 拿 GPU/LLM 许可
            metrics.inc("stream_requests")

            # ② 检索（查询扩展/混合检索/重排，17-25s）在线程池执行
            docs = await run_in_threadpool(service.retrieve, request.question, history)
            context = "\n\n".join(doc.page_content for doc in docs)
            ...
            yield _sse({"event": "sources", "data": sources})  # ③ 先推来源事件

            # ④ 生成阶段：异步流式，逐 token yield
            async for chunk in service.chat.astream(request.question, context, history):
                yield _sse({"event": "token", "data": chunk})  # ⑤ 每 token 一个事件
        finally:
            await run_in_threadpool(service.release)           # ⑥ 无论成败归还许可

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

**逐句解读（面试按这个讲）**：

| 代码 | 为什么 |
|---|---|
| `async def event_generator()` | 异步生成器：整个函数是"边算边给"的协程，每次 yield 一个 SSE 事件 |
| `await run_in_threadpool(service.retrieve, ...)` | **同步重活不能直接调**（会卡事件循环 17s！），扔线程池执行，await 等它完成——等的时候事件循环继续服务其他请求 |
| `yield _sse({"event": "sources", ...})` | 检索完成后先给来源（前端先渲染引用） |
| `async for chunk in service.chat.astream(...)` | 模型逐 token 输出，异步生成器逐个给——**这就是打字机效果的来源** |
| `finally: await run_in_threadpool(service.release)` | 客户端中途断开也要归还信号量许可，防泄漏 |

**黄金法则**：async 端点里的**每一个同步重活都必须包 `run_in_threadpool`**（或 `anyio.to_thread.run_sync`）。判断标准：这个函数会不会阻塞超过 10ms？会就扔线程池。

## 5. 异步生成器 astream（llm.py:114-128）

```python
async def astream(self, question, context, history=None):
    messages = self._build_messages(SYSTEM_TEMPLATE.format(context=context), question, history)
    async for chunk in self._llm.astream(messages, config=self._config()):
        if chunk.content:
            yield chunk.content          # 每次给一段文本
```

- `self._llm.astream` 是 langchain-openai 的异步流式调用（底层是 httpx 异步流）
- 数据流：DeepSeek API 逐 token/分片返回 → astream 逐个 yield → chat_stream 包成 SSE → 前端逐段渲染
- **全程没有任何线程阻塞**：网络等待全是 await 让出

## 6. 同步代码和异步代码怎么共存（你的项目现状）

你的项目是**混合架构**，这是真实世界的常态：

```
同步世界（线程）                          异步世界（事件循环）
┌──────────────────────────┐            ┌──────────────────────────┐
│ 查询扩展（ThreadPool）     │            │ SSE 端点（async def）      │
│ 工具执行（ThreadPool）     │  桥：      │ astream 逐 token           │
│ GPU 推理（信号量限流）      │──────────→│                          │
│ HITL 阻塞（Event）         │run_in_    │                          │
│ MySQL 读写                 │threadpool │                          │
└──────────────────────────┘            └──────────────────────────┘
```

桥的规则（两条）：
1. **async 里调同步重活** → `await run_in_threadpool(fn, ...)`（检索、acquire/release、MySQL）
2. **同步代码里不能调 async**（没有事件循环）→ 要么用 asyncio.run 单独开，要么把同步函数包装（你的项目 LangGraph 主流程是同步的，SSE 端点通过 run_in_threadpool 调它——api/agent.py 就是这么干的）

## 7. asyncio 常用 API 速查

```python
await asyncio.gather(a(), b(), c())          # 并行等全部（面试必问）
task = asyncio.create_task(fn())             # 后台任务
await asyncio.wait_for(coro, timeout=10)     # 带超时（超时抛 TimeoutError）
async for x in agen:                         # 消费异步生成器
await asyncio.sleep(0)                       # 主动让出（协作调度）
```

**项目里 wait_for 的用法**（api/agent.py 的停止机制相关）：流式循环里检查 stop_event 和超时，确保客户端断开后快速收尾。

## 8. 阻塞陷阱：async 里不能做什么

```python
# ❌ 错误：阻塞事件循环
async def bad():
    time.sleep(2)                    # 阻塞 2 秒 → 所有请求都卡 2 秒
    result = service.retrieve(...)   # 同步检索 17s → 全站卡 17s
    data = requests.get(url)         # 同步网络库 → 卡
    open("big.pdf").read()           # 大文件读取 → 卡

# ✅ 正确
async def good():
    await asyncio.sleep(2)           # 异步 sleep，让出
    result = await run_in_threadpool(service.retrieve, ...)   # 重活扔线程池
    data = await httpx.AsyncClient().get(url)                 # 异步网络库
    content = await run_in_threadpool(open("big.pdf").read)   # 文件也扔线程池
```

**诊断方法**：如果某个并发场景"一个慢请求导致全站变慢"，先查 async 函数里有没有同步阻塞调用。你的项目检索慢（17s）但不影响其他请求，就是因为包了 run_in_threadpool。

## 9. 项目异步全景 + 面试故事

面试版本：*"流式问答用 FastAPI 的 async 端点：事件循环跑 SSE 协程，同步重活（检索 17 秒、信号量获取）全部包 run_in_threadpool 扔线程池，生成阶段用 langchain 的异步流式逐 token yield，前端拿到打字机效果。关键纪律是 async 函数里绝不直接调同步阻塞函数——会卡住整个事件循环。我的项目是同步（LangGraph 编排、线程池、信号量）和异步（SSE 流式）混合架构，中间用 run_in_threadpool 做桥。"*

## 10. 练习

1. 写 `async def wait_and_return(i): await asyncio.sleep(1); return i`，用 gather 并行跑 5 个，测总耗时 ≈1s（串行是 5s）
2. 用 `run_in_threadpool` 在 async 函数里调一个 `time.sleep(2)` 的同步函数，证明事件循环不被卡（并发两个请求总耗时 ≈2s 而非 4s）
3. 写一个最小 SSE 端点（FastAPI）：`yield` 5 个事件，前端 fetch 逐段打印
4. 找出你项目 api/chat.py 里所有 `run_in_threadpool` 的调用点，逐个说"为什么这个要扔线程池"

## 11. 自测清单

- [ ] 能解释协程 await 时发生了什么（让出给事件循环）
- [ ] 能解释为什么 SSE 流式必须 async（长连接不占线程）
- [ ] 知道 async 函数里调同步重活的两个选择（run_in_threadpool / 异步库）
- [ ] 能画出项目"同步世界 ↔ 异步世界"的桥
- [ ] 能讲清打字机效果的数据流（模型 → astream → SSE → 前端）
