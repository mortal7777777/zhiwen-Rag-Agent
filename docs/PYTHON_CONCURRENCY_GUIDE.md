# 并发编程概念总览：线程、进程、协程怎么选

> 目标：建立并发心智模型——先理解"为什么"和"怎么选"，再学细节（细节在 THREADING 和 ASYNC 两篇）。
> 你的项目是绝佳教材：并发三件套（线程池/信号量/协程）都用到了，每一处都有真实理由。

---

## 1. 为什么要并发？先分清两类任务

程序等待分两种：

```
类型 A：IO 密集（等外部）
  读文件、调数据库、请求大模型 API、网络等待……
  CPU 几乎不干活，时间都花在"等"上（等磁盘、等网络、等对方响应）

类型 B：CPU 密集（自己算）
  大量计算、循环、embedding 向量运算、数据排序……
  CPU 一直在忙
```

**你的项目里绝大多数操作是 IO 密集**：调 DeepSeek API（查询扩展、回答生成，每次 3~20 秒都在等网络）、读写 MySQL、OpenSearch 检索、加载模型文件。CPU 密集的只有 GPU 推理（那是显卡在算，Python 只是调度）。

并发解决的核心问题：**等 IO 的时候别闲着**。一个请求在等 DeepSeek 返回时，另一个请求应该能用 CPU 干自己的事。

## 2. 三兄弟：线程、进程、协程

| | 线程（thread） | 进程（process） | 协程（coroutine） |
|---|---|---|---|
| 本质 | 进程内独立执行流，共享内存 | 独立程序实例，内存隔离 | 单个线程内"协作式"切换的函数 |
| 切换方式 | 操作系统调度（抢占式） | 操作系统调度 | 代码自己让出（await 处） |
| 切换开销 | 中等 | 大（创建进程很重） | 极小 |
| 共享数据 | 容易（同一进程内存），但要注意线程安全 | 难（要 IPC） | 同线程内天然安全 |
| GIL 影响 | 有（CPU 密集多线程不加速） | 无（每个进程独立 GIL） | 有（但等 IO 时自动让出） |
| 适合 | IO 密集 + 需要并行 | CPU 密集 / 隔离要求高 | IO 密集 + 大量连接（网络服务） |
| Python 实现 | threading 模块 | multiprocessing 模块 | asyncio 模块 |

### 关键术语

- **并发（concurrency）**：多个任务"看上去同时"推进——交替执行。一个 CPU 也能并发。
- **并行（parallelism）**：多个任务"真的同时"执行——需要多核。多核 CPU 上多进程/多线程可以并行。
- **阻塞（blocking）**：调用不返回，线程卡在那里等结果。
- **非阻塞（non-blocking）**：调用立即返回，结果好了再通知你。

## 3. GIL：Python 线程的紧箍咒

**GIL（全局解释器锁）**：CPython 解释器同一时刻只允许**一个线程**执行 Python 字节码。

影响：
- **CPU 密集的多线程不加速**：两个线程想同时算，GIL 只放行一个，来回切换反而更慢 → CPU 密集用**多进程**（每个进程独立解释器、独立 GIL）。
- **IO 密集不受影响**：线程等 IO 时自动释放 GIL，其他线程趁机执行 → 所以 Python 里 IO 密集用多线程/协程完全可行。

一句话记住：**GIL 锁的是"解释器执行代码"，不锁"等 IO"**。你的项目全是等 IO，所以多线程/协程有效。

## 4. 你的项目并发使用地图（面试背这张表）

| 位置 | 用了什么 | 为什么 |
|---|---|---|
| `app/rag/service.py:53-56` | `threading.BoundedSemaphore` 两个信号量（GPU 许可 + LLM 许可） | embedding/reranker 共享 GPU，限制并发防止打满；LLM API 单独限流防 429 |
| `app/rag/query_expander.py:72-113` | `ThreadPoolExecutor(max_workers=3)` | Multi-Query/HyDE/多轮补全三个 LLM 调用并行发，把扩展耗时从 ~19s 降到 ~7s |
| `app/agent/langgraph_agent.py:2171-2173` | `ThreadPoolExecutor` 并行执行多个工具调用 | 模型一次返回多个 tool_calls 时并行执行，快几倍 |
| `app/permissions.py:169/228` | `threading.Event` | HITL：工具线程阻塞等用户审批，resolve 后置位唤醒 |
| `app/agent/langgraph_agent.py:1982/3162` | `threading.Thread` 后台线程 | 深度思考摘要后台预生成；标题后台生成，不阻塞主流程 |
| `app/api/chat.py:44` `api/agent.py` | `async def` + `run_in_threadpool` | SSE 流式：一个请求一个协程，等 IO 时让出给其他请求；同步重活（检索）扔线程池 |
| `app/rag/llm.py:114` | `async def astream`（异步生成器） | 逐 token 流式返回给前端 |
| `app/todos.py:280-286` | 每个操作独立短会话 | 工具并行执行时共用请求级连接会把 MySQL 连接搞坏（真实踩坑） |

**面试版总结**：我的项目是混合模型——异步端点（SSE 流式、高并发 IO）＋ 线程池（并行 LLM 调用和工具执行）＋ 信号量（GPU/API 限流）＋ Event（HITL 唤醒）＋ 每任务独立数据库连接（避免并发连接损坏）。**每一种用在哪都有理由，不是堆技术**。

## 5. 怎么选：决策树

```
任务类型是什么？
├─ CPU 密集（纯计算）→ 多进程 multiprocessing（GIL 会拖后腿）
├─ IO 密集，且要处理大量连接 → 协程 asyncio（一个线程扛千个连接）
├─ IO 密集，任务数量中等 → 线程池 ThreadPoolExecutor（简单直观）
└─ 有共享状态要保护 / 要阻塞等待事件 → threading 的 Lock/Event/Semaphore
```

经验法则：
- **Web 服务（FastAPI）**：默认异步端点；同步重活扔线程池（`run_in_threadpool`）——你的项目就是这么做的
- **批量调 API**：线程池或 asyncio.gather（你的查询扩展用线程池，因为要兼容同步代码）
- **GPU 共享**：信号量限流（你的 embedding/reranker）
- **等用户输入**：Event（你的 HITL）

## 6. 并发三大坑（面试必考 + 实战必踩）

### 坑 1：竞态条件（Race Condition）
两个线程同时读写同一份数据，结果取决于谁先谁后，不可预测。

```python
# 错误示范（todos 并发 bug 的根源）
def update():
    items = load_todos(shared_connection)   # 两个线程同时用同一个连接
    items.append(new)                        # 连接状态互相踩
    save_todos(shared_connection, items)

# 正确做法：每个任务独立连接（你的修复）
def update():
    with SessionLocal() as local:            # 独立短会话
        items = load_todos(local, ...)
        ...
```

### 坑 2：死锁（Deadlock）
两个线程互相等对方持有的锁，谁也不让。

```python
# 线程 A 拿锁1 等锁2，线程 B 拿锁2 等锁1 → 永远等下去
```
解法：锁的获取顺序全局一致；尽量用更小的临界区；`with lock:` 保证释放。

### 坑 3：阻塞事件循环（协程的坑）
协程里调用同步阻塞函数，会把整个事件循环卡住，其他请求全部变慢。

```python
# 错误：async 函数里直接调同步重活
async def handler():
    docs = service.retrieve(...)     # 同步检索 17s → 事件循环卡死 17s，所有请求排队！

# 正确：扔线程池
async def handler():
    docs = await run_in_threadpool(service.retrieve, ...)   # 卡的是线程池线程，事件循环畅通
```

## 7. 并发面试高频题（先自答再看答案）

1. **GIL 是什么？多线程到底有没有用？** → 见 §3。IO 密集有用，CPU 密集没用（用多进程）。
2. **线程和协程的区别？** → 线程是操作系统调度（抢占式），协程是代码自己让出（协作式）；协程切换开销极小，适合大量 IO 连接；线程受 GIL 约束且切换有成本。
3. **什么时候用 asyncio？** → IO 密集 + 大量并发连接（Web 服务、SSE 流式）；代码要全程 async，第三方同步库要包线程池。
4. **信号量（Semaphore）和锁（Lock）的区别？** → 锁一次只放一个（互斥）；信号量可以放 N 个（限流）。项目里 GPU 许可就是"最多 N 个请求同时用 GPU"。
5. **Event 是什么？** → 一个线程设标志，其他线程等它。HITL 审批、停止信号都用它。
6. **进程间通信？** → Queue/Pipe/共享内存；分布式用消息队列。你的项目单机单进程，不用关心。
7. **怎么调试并发问题？** → 日志带线程名（logging 默认有）；复现要并发压力测试（你的项目有 concurrency_test.py）；数据库并发问题看连接层报错。

## 8. 自测清单

- [ ] 能说出线程/进程/协程各自适合什么，举出项目例子
- [ ] 能解释 GIL 为什么不影响"等 IO"
- [ ] 能解释你的项目为什么"异步端点 + 线程池 + 信号量"混合
- [ ] 能讲清 MySQL packet sequence bug 的竞态根因和修复方式
- [ ] 知道 async 函数里调同步重活要用 run_in_threadpool

---

下一篇：`PYTHON_THREADING_GUIDE.md`（threading/Event/信号量/线程池实战）
