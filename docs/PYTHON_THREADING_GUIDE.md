# 线程与线程池实战（结合项目代码逐段解读）

> 前置：先读 `PYTHON_CONCURRENCY_GUIDE.md`（概念总览）。
> 目标：看懂并会用 threading 的 Thread/Lock/Event/Semaphore 和 ThreadPoolExecutor，能解释你项目里每一处线程代码"为什么这么写"。
> 代码对照：所有片段都能在 backend/app/ 下找到。

---

## 1. threading 基础：创建线程

```python
import threading
import time

def work(name: str):
    print(f"{name} 开始")
    time.sleep(2)          # 模拟 IO 等待
    print(f"{name} 结束")

# 创建并启动线程
t = threading.Thread(target=work, args=("任务A",), daemon=True)
t.start()                  # 启动后立即返回，work 在子线程里跑

# 主线程继续干别的...
print("主线程继续")

# 等子线程结束（不 join 程序可能直接退出）
t.join()
```

关键点：
- `target` 是要执行的函数，`args` 是参数元组
- `start()` 立刻返回（非阻塞），函数在后台线程执行
- `join()` 阻塞等线程结束
- `daemon=True`：守护线程——主程序退出时不等待它（项目里标题生成线程就是 daemon，:335）

**项目例子（langgraph_agent.py:1982-1994）**——深度思考摘要后台预生成：

```python
evt = {"summary": None, "done": threading.Event()}   # 预生成线程和主流程用 Event 通信
def _pre_generate():
    try:
        evt["summary"] = ...生成摘要(最长 120 字)...
    finally:
        evt["done"].set()          # 完成标志置位
threading.Thread(target=_pre_generate, daemon=True).start()
# 主流程不阻塞等待；正文要展示摘要时最多等 1 秒（:1273）
evt["done"].wait(timeout=1.0)      # 等到就展示，没等到就不展示（超时放行）
```

**这段代码的价值**：工具执行期间（几秒到几十秒）后台偷偷把思考摘要算好，用户看到"已深度思考"折叠区——**后台线程做预计算，Event 做完成通知，主流程超时兜底**。面试问"你用过线程吗"就讲这个。

## 2. Lock：互斥锁

多个线程同时改同一个数据会互相踩（竞态）。Lock 保证"同一时刻只有一个线程进入这段代码"。

```python
class PermissionManager:
    def __init__(self):
        self._requests: dict[str, PendingRequest] = {}
        self._lock = threading.Lock()          # 互斥锁（permissions.py:191）

    def resolve(self, request_id, approve, ...):
        with self._lock:                       # 进入锁：别人进不来
            req = self._requests.get(request_id)
            if req is None or req.status != "pending":
                return None
            req.remember_forever = remember_forever
        # 出锁后继续（锁的范围越小越好——只保护共享数据读写）
        self._mark(req, approve, reason)
```

**为什么需要锁**：多个请求同时调 resolve（用户快速连点、CLI 和 Web 同时审批），两个线程同时改 `_requests` 或同一个 req 的状态，可能丢更新。锁保证"读-判断-写"是原子的。

**锁的纪律**：
- 只保护**共享可变数据**（锁的范围越小越好）
- 永远 `with lock:`（异常也会释放，不会死锁）
- 不要嵌套拿两把锁（易死锁）

## 3. Event：事件通知（HITL 的核心）

Event 是"一个线程设标志，其他线程等标志"的跨线程通信工具。四个方法：
- `e.set()`：置位（通知）
- `e.clear()`：复位
- `e.wait(timeout)`：阻塞等待置位（可带超时）
- `e.is_set()`：看是否已置位

**项目例子 1：HITL 审批（permissions.py:211-233）**

```python
def wait(self, req, timeout=300, stop_event=None) -> bool:
    deadline = None if timeout <= 0 else time.time() + max(1, timeout)
    while True:
        if stop_event is not None and stop_event.is_set():
            self._mark(req, False, "用户停止了回答")    # 客户端断了 → 自动拒绝
            return False
        if req._event.wait(timeout=0.5):                # 每 0.5 秒醒来看一眼
            break                                       # 用户点了 → 唤醒
        if deadline is not None and time.time() >= deadline:
            self._mark(req, False, "等待确认超时，已自动取消")
            return False
    return bool(req.decision)
```

**为什么是"轮询 + 超时"而不是一直 wait**：要同时响应三件事——用户决定（Event 置位）、停止信号（stop_event）、超时。所以每 0.5 秒醒来检查一次，任何条件满足就退出。这是"多条件等待"的标准写法（Event 一次只能等一个，多条件用轮询）。

完整链路（背这个，面试画时序图）：
```
工具线程：submit() 注册请求 → SSE 推审批卡 → wait() 阻塞轮询
用户：    前端点批准 → POST /api/agent/permission/{id}/resolve
后端：    resolve() → _mark() 改状态 → req._event.set() 置位
工具线程：wait() 醒来 → 拿到 decision → 执行 or 把拒绝原因回传模型
```

**项目例子 2：停止信号（stop_event）**——客户端断开/Ctrl+C 时，SSE 连接关闭 → 取消接口置位 stop_event → 所有 wait/循环检查它后快速收尾，不挂死。

## 4. Semaphore / BoundedSemaphore：限流

信号量 = "最多 N 个通行证"。`acquire()` 拿一张（没有就等），`release()` 还一张。

**项目例子（service.py:52-56, 192-206）**：

```python
self._semaphore = threading.BoundedSemaphore(settings.max_concurrency)  # GPU 许可，默认 2
self._llm_semaphore = threading.BoundedSemaphore(settings.llm_max_concurrency)  # LLM 许可

def acquire_gpu(self): self._semaphore.acquire()   # 拿 GPU 许可（没有就排队等）
def release_gpu(self): self._semaphore.release()   # 用完归还
def acquire_llm(self): self._llm_semaphore.acquire()
def release_llm(self): self._llm_semaphore.release()
```

**为什么需要**：
- GPU（RTX 4060 8G）同时只能跑几个 embedding/rerank 推理，并发太多会显存溢出/CUDA OOM——所以 embedding 和 rerank 段先拿 GPU 许可
- DeepSeek API 有限流（429），LLM 并发太多会触发限流——所以 API 调用单独限流

**信号量 vs 锁**：锁是"一次一个"（互斥）；信号量是"一次 N 个"（限流）。GPU 许可=限流 2，不是互斥 1。

用法纪律：`acquire` 和 `release` 必须成对（**try/finally 保证 release**，否则异常时许可永久丢失，系统慢慢饿死）：

```python
service.rag.acquire_llm()          # langgraph_agent.py:1284
try:
    stream = chat.stream(messages, config=...)
    for chunk in stream:
        ...
finally:
    service.rag.release_llm()      # 无论成败都要归还（:1324）
```

## 5. ThreadPoolExecutor：线程池（批量并行）

手搓线程麻烦（要管理生命周期、异常、收集结果）。线程池统一管理：任务扔进去，池子里 N 个线程轮流执行。

```python
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor(max_workers=3) as pool:
    future = pool.submit(fn, arg1, arg2)   # 提交任务，立即返回 future
    result = future.result()               # 阻塞等结果（拿不到会等）
```

**项目例子 1：查询扩展三路并行（query_expander.py:72-113）**

```python
futures: dict[str, object] = {}
with ThreadPoolExecutor(max_workers=3) as pool:
    if self._use_multi_turn and history:
        futures["disambiguate"] = pool.submit(self._chat.disambiguate, question, history)
    if not simple:
        if self._use_multi_query:
            futures["rewrite"] = pool.submit(self._chat.rewrite_queries, question, history, n=self._variants)
        if self._use_hyde:
            futures["hyde"] = pool.submit(self._chat.hypothetical_document, question, history)

    # 三个 LLM 调用已经并行发出，各自等网络返回
    if "disambiguate" in futures:
        try:
            standalone = futures["disambiguate"].result()   # 第一个结果
        except Exception as exc:
            logger.warning("多轮补全失败：%s", exc)
    if not simple:
        if "rewrite" in futures:
            try:
                rewritten = futures["rewrite"].result()
            except Exception as exc:
                logger.warning("Multi-Query 失败：%s", exc)
        ...
```

**这段代码的价值**：三个独立的 DeepSeek 调用（改写/HyDE/消歧）互不依赖，串行要 3× 耗时（约 19 秒），并行只要最慢那个（约 7 秒）。**IO 密集并行 = 总耗时从"相加"变"取最大"**。每个 future 都单独 try/except——一个失败不影响其他（失败降级哲学）。

**项目例子 2：工具并行执行（langgraph_agent.py:2171-2173）**

```python
with ThreadPoolExecutor(max_workers=min(3, max(1, len(tool_calls)))) as pool:
    # 模型一次返回多个工具调用（如同时读 3 个文件）→ 并行执行
```
`min(3, len(tool_calls))`：任务少就少开线程（不浪费），最多 3 个。

**面试题"线程池和手搓线程区别"**：线程池复用线程（不用每次创建销毁）、统一管理排队、future 统一收集结果和异常；手搓线程适合"一个明确的后台任务"（标题生成）。

## 6. 线程安全与数据库连接：你踩过的真坑

**坑的背景（todos.py:280-286 + langgraph_agent.py:2171）**：
工具并行执行时，多个线程**共用同一个请求级数据库 Session**。MySQL 连接是有状态的协议（包序号、事务），两个线程同时发请求 → 连接上的包序混乱 → `Packet sequence number wrong` → 连接损坏，后续所有数据库操作全失败。

**修复（每个工具调用用独立短会话）**：

```python
def _invoke(operation, text="", ...):
    local = None
    try:
        from .db.database import SessionLocal, db_ready
        if db_ready and SessionLocal is not None:
            local = SessionLocal()          # 每个工具调用自己的连接
        items = load_todos(local, conversation_id)
        ...操作...
        save_todos(local, conversation_id, items)
    finally:
        if local is not None:
            local.close()                   # 用完即关
```

**通用教训（面试讲这个很加分）**：
> 并发安全分两层：**数据层**（同一个对象被多线程改 → 用锁）和**资源层**（有状态的外部资源如数据库连接被多线程共用 → 各自独立实例）。连接不是线程安全的容器，它是有状态通道——多线程共用要么加锁排队（浪费），要么每任务独立（推荐）。

## 7. 项目线程使用全景（面试讲"我用过线程"的完整故事）

1. **限流**：BoundedSemaphore 管 GPU（2）和 LLM API（并发上限）
2. **并行 IO**：ThreadPoolExecutor 跑查询扩展三路（19s→7s）和多工具调用
3. **后台任务**：daemon 线程预生成思考摘要/标题，Event 通知主流程
4. **等待人**：Event 实现 HITL 阻塞 + 超时 + 停止信号三条件等待
5. **资源隔离**：工具并行时每任务独立数据库会话（踩坑修复）

面试版本：*"我项目里线程用在五处：信号量限流 GPU 和 API、线程池并行查询扩展和工具调用、后台线程预生成摘要、Event 做审批阻塞和停止信号、每个工具调用独立数据库会话。印象最深的是并发把 MySQL 连接搞坏的 bug——连接是有状态通道，多线程共用必须隔离。"*

## 8. 练习（每题 10 分钟）

1. 用 BoundedSemaphore(2) 模拟 GPU 许可：10 个任务抢 2 个许可，打印同时运行数 ≤2
2. 用 ThreadPoolExecutor(3) 并行请求 3 个 URL（用 urllib），比较串行/并行耗时
3. 模拟 HITL：主线程 submit 请求后 wait，另一个线程 sleep(2) 后 set()，观察唤醒
4. 改 bug：两个线程同时 `threading.Thread(target=increment)` 各加 100000 次同一个全局变量，观察结果（不是 200000）——用 Lock 修复

## 9. 自测清单

- [ ] 能写出 Thread/Event/Semaphore/ThreadPoolExecutor 的最小可用代码
- [ ] 能解释项目里 HITL 的 wait 为什么轮询而不是一直阻塞
- [ ] 能解释 acquire/release 为什么要 finally
- [ ] 能讲清 MySQL packet sequence bug 的根因和修复
- [ ] 知道锁的范围越小越好、连接要独立实例

---

下一篇：`PYTHON_ASYNC_GUIDE.md`（协程与 asyncio 实战）
