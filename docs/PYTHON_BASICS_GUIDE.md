# Python 基础查漏补缺（面向项目代码阅读）

> 目标：读得懂你的 rag 项目代码。不教全部 Python，只教**项目里真实出现的高频语法**，每个概念配项目真实代码 + 通俗解释。
> 用法：边读边在 PyCharm 里打开对应文件对照；每节后的练习 10 分钟内完成。
> 前置：会写最基础的 Python（变量、if/for、函数、class 略懂）。

---

## 1. 数据结构：dict 的五个高频操作

项目里 dict 用得最多（查 `app/rag/retriever.py` 的 RRF 融合就是 dict 操作）：

```python
# ① setdefault：没有就设默认值，有就不动。返回该 key 的值
item = merged.setdefault(doc.page_content, {"doc": doc, "rrf": 0.0})
# 等价于：
if doc.page_content not in merged:
    merged[doc.page_content] = {"doc": doc, "rrf": 0.0}
item = merged[doc.page_content]

# ② 遍历时取 key 和 value
for rank, doc in enumerate(docs, 1):   # enumerate：拿序号，从 1 开始
    ...

# ③ get 带默认值（读 dict 不报 KeyError）
source = doc.metadata.get("source") or "?"

# ④ sorted + key 按值排序（最常用！）
ranked = sorted(merged.values(), key=lambda item: item["rrf"], reverse=True)

# ⑤ 统计计数（项目里统计候选来源分布）
from collections import Counter
Counter(d.metadata.get("source") for d in docs).most_common()
```

**集合 set**：去重专用。

```python
seen = set()
for q in queries:
    if q not in seen:
        seen.add(q)
```

**deque（双端队列）**：两端都能取，项目里做"按来源轮流取候选"（retriever.py source 分流）：

```python
from collections import deque
pending = deque(["pdf", "epub", "jiang"])
src = pending.popleft()      # 取左边
pending.append(src)          # 放回右边 → 实现轮流
```

### 练习 1
用 dict 统计一段文本里每个词出现次数（用 setdefault 或 Counter 都行）。

## 2. 推导式：一行生成列表/字典

```python
# 列表推导（项目：构建 sources 列表）
sources = [
    {"content": doc.page_content, "score": doc.metadata.get("rerank_score")}
    for doc in docs
]

# 带条件的推导（项目：过滤空查询）
queries = [q for q in queries if q.strip()]

# 字典推导（项目：工具名→工具对象映射）
by_name = {t.name: t for t in tools}

# 生成器表达式（不建列表，省内存，只遍历一次）
sum(1 for i in items if i.get("done"))
```

**面试题"推导式和普通循环区别"**：推导式更简洁，本质是一个表达式构造容器；生成器表达式惰性求值（用到才算），大列表省内存。

## 3. *args / **kwargs：不确定参数

```python
def _timed_node(name: str, fn):      # fn 是个函数，函数也能当参数传！
    def wrapped(state: AgentState) -> dict:
        ...
        return fn(state)             # 包装器：执行前记时，执行后算耗时
    return wrapped
```

`**kwargs` 项目里：`build_tools(rag, use_web_search, ..., searxng_base_url=..., searxng_engines=...)` 传参。函数参数里的 `**kwargs` 表示"接收所有未命名的关键字参数"。

## 4. 装饰器：给函数"套壳"

项目真实例子（langgraph_agent.py:2862）：

```python
def _timed_node(name, fn):
    """节点耗时打点：把 fn 的执行时间累加进 timings。"""
    def wrapped(state):
        runtime = state.get("runtime")
        timings = runtime.setdefault("timings", {})
        t0 = time.perf_counter()          # 开始计时
        try:
            return fn(state)              # 执行原函数
        finally:
            timings[f"{name}_ms"] = timings.get(f"{name}_ms", 0) + (time.perf_counter() - t0)
    return wrapped

# 使用：给每个图节点包一层计时
graph.add_node("agent", _timed_node("agent", _agent_node))
```

**本质**：装饰器 = 一个返回函数的函数。`@property` 也是装饰器（service.py 的懒加载）：

```python
@property
def store(self) -> OpenSearchStore:
    if self._store is None:            # 第一次访问才创建（懒加载）
        self._store = OpenSearchStore(...)
    return self._store                 # 之后直接用缓存
```

**为什么懒加载**：模型加载要几秒，启动时就加载会拖慢启动；用 property + None 判断，第一次用到才加载。

## 5. 生成器与迭代器：yield

```python
# 普通函数：算完返回
def gen():
    result = []
    for i in range(10):
        result.append(i * 2)
    return result

# 生成器：边算边给（惰性）
def gen():
    for i in range(10):
        yield i * 2      # 到这里暂停，给出去，下次继续

for x in gen():          # for 循环每次拿一个
    print(x)
```

项目里的生成器（api/chat.py 的 SSE 流式）——**每次 yield 一个 SSE 事件字符串，前端收到就渲染**：

```python
async def event_generator():
    ...
    yield _sse({"event": "sources", "data": sources})   # 先推来源
    async for chunk in ...:
        yield _sse({"event": "token", "data": chunk})   # 再逐 token 推
```

**关键认知**：SSE 打字机效果 = 生成器逐段 yield + 网络逐段推送 + 前端逐段渲染。普通函数只能"全部算完一次给"，生成器能"算一点给一点"。

## 6. 类型提示（Type Hints）

```python
def load_todos(db: Session | None, conversation_id: int | None) -> list[dict]:
    # db 可能是 Session 或 None；conversation_id 可能 None；返回 list[dict]
    ...

state: AgentState | None = state.get("runtime")
```

`X | None` 表示"可能是 X 也可能是 None"（Python 3.10+ 语法），函数签名里表示"这个参数可空"。类型提示不强制，但让代码可读、IDE 提示、mypy 检查。

## 7. 上下文管理器：with

```python
# 项目：独立数据库短会话（用完自动关连接）
local = SessionLocal()
try:
    items = load_todos(local, conversation_id)
finally:
    local.close()

# with 写法等价（更安全，异常也会关）：
with SessionLocal() as local:
    items = load_todos(local, conversation_id)
```

`with` 的本质：进入时调用对象的 `__enter__`，退出时（含异常）调用 `__exit__`。文件、数据库连接、锁都支持。**面试题"with 的作用"**：保证资源（文件/连接/锁）一定被释放，即使中间抛异常。

## 8. 异常处理与日志

```python
try:
    queries = self.query_expander.expand(question, history)
except Exception as exc:                 # 捕获所有异常
    logger.warning("查询扩展失败，退化为原问题：%s", exc)   # 记日志但不崩
    queries = [question]                 # 降级：用原问题继续

try:
    ...
except Exception:
    pass                                 # 吞掉（项目里懒加载失败场景）
finally:
    local.close()                        # 无论成败都执行
```

**项目里为什么到处 try/except**：LLM 调用、数据库、模型加载都可能失败，RAG 链路的哲学是"**失败降级**"——扩展失败用原问题、检索失败返回空、审批失败自动拒绝，不让一个环节的失败毁掉整个请求。

## 9. 常用标准库（项目高频）

```python
from pathlib import Path              # 路径操作
Path("data/books").resolve()          # 绝对路径
data_dir / filename                   # 路径拼接

import json
json.dumps(data, ensure_ascii=False, indent=2)   # 中文不转义
json.loads(text)

import hashlib                        # 指纹/去重
hashlib.md5(text.encode("utf-8")).hexdigest()
hashlib.sha256(raw).hexdigest()

import uuid
uuid.uuid4().hex[:8]                  # 随机 ID（todos 的 todo_xxxx）

import time
time.time()                           # 时间戳
time.perf_counter()                   # 高精度计时（性能打点）

import re
re.split(r"(?<=[。！？；])", text)      # 中文按句号切分（splitter.py）
re.search(r"\[.*\]", content, re.DOTALL)
```

## 10. 模块导入：from . 和懒加载

```python
# 相对导入：from . 表示当前包（rag），.. 表示上级包（app）
from .embeddings import LocalBGEEmbeddings   # app/rag/embeddings.py
from ..config import Settings                # app/config.py

# 函数内导入（懒加载）——项目标配：
def query_expander(self):
    if self._query_expander is None:
        from ..runtime_config import effective    # 用到才导入
        ...
```

**为什么函数内 import**：① 打破循环依赖（A 导入 B、B 又导入 A 会报错）；② 懒加载重型依赖（torch 只在用到时加载）。面试题"函数内 import 和顶部 import 区别"：顶部是模块级一次加载；函数内是调用时加载，适合可选依赖/循环依赖。

## 11. 真实代码逐行解读（拿两段练手）

**段一：RRF 融合（retriever.py:56-67）**

```python
merged: dict[str, dict] = {}                       # 1. 结果容器
for docs in query_results:                         # 2. 遍历每个查询的结果
    for rank, doc in enumerate(docs, 1):           # 3. 排名从 1 开始
        item = merged.setdefault(                  # 4. 按内容去重取条目
            doc.page_content, {"doc": doc, "rrf": 0.0})
        item["rrf"] += 1.0 / (RRF_K + rank)        # 5. 累加融合分
ranked = sorted(merged.values(),                   # 6. 按融合分从高到低
                key=lambda item: item["rrf"], reverse=True)
result = []
for item in ranked[:limit]:                        # 7. 取前 N 个
    doc = item["doc"]
    doc.metadata["query_merge_score"] = round(item["rrf"], 4)
    result.append(doc)
return result
```

逐句翻译：把多个查询的检索结果按"文本内容"合并 → 同一个文本被越靠前的排名命中越多，融合分越高 → 排序取前 N。

**段二：todos 工具操作（todos.py:287-316 摘要）**

```python
local = None
try:
    from .db.database import SessionLocal, db_ready   # 工具里才导入
    if db_ready and SessionLocal is not None:
        local = SessionLocal()                        # 独立短会话
    items = load_todos(local, conversation_id)        # 读清单
    op = (operation or "list").strip().lower()        # 参数规范化
    if op == "add":
        if not text.strip():
            return {"error": "参数缺失：add 需要 text", ...}
        txt = text.strip()[:200]                      # 截断防超长
        if any(str(i.get("text", "")).strip() == txt for i in items):
            return {"summary": "任务已存在，跳过重复添加", ...}   # 去重
        items.append({"id": f"todo_{uuid.uuid4().hex[:8]}", ...})
        save_todos(local, conversation_id, items)     # 写库
        return {"summary": f"已新增任务：{txt[:40]}", "todos": items}
finally:
    if local is not None:
        local.close()                                 # 无论成败关连接
```

## 12. 自我检测清单

- [ ] 能说出 setdefault / get / set 的用途
- [ ] 能写出一个带条件过滤的列表推导式
- [ ] 能解释装饰器是"返回函数的函数"，举出项目里 _timed_node 的例子
- [ ] 能解释生成器"边算边给"和 SSE 流式的关系
- [ ] 能说出 with 的保证和 finally 的区别
- [ ] 能在项目代码里指出一个"失败降级"的 try/except

## 13. 面试常见 Python 基础题（附简答）

1. **== 和 is 的区别**：== 比内容，is 比内存地址（小整数/短字符串有驻留池，别用 is 比内容）。
2. **可变/不可变**：list/dict/set 可变（函数内改会改到外面，作为默认参数有坑）；str/tuple/int 不可变。
3. **浅拷贝 vs 深拷贝**：`copy.copy` 只复制外层，`copy.deepcopy` 递归复制。项目里 `dict(parent_meta)` 是浅拷贝一层，够用。
4. **GIL 是什么**：CPython 解释器同一时刻只允许一个线程执行字节码——见并发文档。
5. **异常层级**：BaseException → Exception → 具体异常；捕获用 Exception 别用裸 except（会连 KeyboardInterrupt 一起吞）。
6. **None 判断**：`x is None`（不要 `x == None`）；`or` 有陷阱——`0 or "" or None` 都算假值，`metadata.get("source") or "?"` 里 source 为空字符串也会被替换成 "?"。
