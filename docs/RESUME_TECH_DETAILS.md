# 简历技术点详解：智能知识管理 Agent 系统

> 本文把简历中出现的每个技术点，对照本项目（`rag_knowledge_base`）的真实实现逐项讲解，
> 标注代码位置与关键代码片段，供面试前复习与查漏补缺。
> 阅读方式：先读 §2 总览建立全局，再按小节逐项过；每个小节都能独立讲 1~2 分钟。

---

## 1. 简历技术点 → 文档章节映射

| 简历条目 | 对应章节 |
|---|---|
| 文档解析入库、智能切分、增量更新 | §3、§4 |
| 向量化与知识库构建 | §5 |
| 混合检索（BM25+语义向量）、重排序 | §6 |
| 查询扩展、联网兜底（CRAG） | §7、§9 |
| 来源编号引用 | §8 |
| RAGAS 评估与回归门槛 | §10 |
| LangGraph 多节点 Agent、子任务分解、任务清单硬约束、审批流、断点恢复 | §11~§15 |
| MCP 接入 Playwright | §16 |
| 技能按需扫描注入 | §17 |
| 前缀缓存优化（95%+） | §18 |
| Vue 前端交互 | §19 |
| 工程化（测试/可观测） | §19 |

---

## 2. 系统总览

**一句话定位**：基于 RAG 的个人知识库问答系统——文档入库后，用户提问经"检索→重排→生成"链路得到带来源引用的回答；复杂任务由 LangGraph Agent 编排多轮工具调用完成。

**一次知识库问答的数据流**：

```text
用户提问
  → 查询扩展（Multi-Query / HyDE / 多轮补全，简单问题跳过）
  → 混合检索（OpenSearch kNN 向量 + BM25 关键词，各召回 Top 40）
  → RRF 融合（排名融合出 Top 24）
  → Small-to-Big 聚合（子块 → 父块补全上下文）
  → 本地 reranker 精排（取 Top 4）
  → 相关度不足时 CRAG 联网兜底
  → LLM 生成回答（带 [n] 来源编号引用）
  → 来源去重 + 附录补全 → 返回前端
```

**核心代码**：
- 检索管线：`backend/app/rag/service.py`（`RAGService.retrieve`，入口）
- 编排：`backend/app/agent/langgraph_agent.py`（Agent 图）

---

## 3. 文档解析入库

**原理**：不同文档格式的解析方式完全不同，需要按格式分发：文本类直接读、PDF 需要版面分析、EPUB 本质是 ZIP（XHTML 章节）、老式 .doc 是二进制需专用解析器。解析结果统一为 LangChain `Document`（content + metadata），metadata 携带来源文件名/页码，供后续引用与索引。

**项目实现**：
- 支持 txt / md / csv / doc / docx / xlsx / pdf / epub 八种格式；
- PDF 用 PyMuPDF4LLM 做布局感知解析（多栏、页眉页脚按版面还原），扫描件（无文字层）可配置 SenseNova OCR 兜底；书签 GBK 编码检测转码修复乱码；
- 每个文档带 `relative_path` 元数据，来源引用能精确定位到文件；
- **增量索引**：文件内容指纹（哈希）对比，没变直接复用已有向量，只新增嵌入新文件，修改/删除才重建。

**代码位置**：
- `backend/app/rag/loader.py`：`load_documents()`（统一入口）、`load_pdf()`、`_layout_markdown_parallel()`（版面 PDF）、`load_doc()`（按扩展名分发）
- 增量索引与指纹：`backend/app/rag/service.py`（`build_index` / `ensure_index`）

**关键代码**（loader.py 按扩展名分发）：

```python
def load_documents(data_dir: Path) -> list[Document]:
    docs = []
    for file_path in list_data_files(data_dir):
        docs.extend(load_doc(file_path))   # 内部按扩展名走对应解析器
    return docs
```

---

## 4. 智能切分：父子分块

**原理**：一段长文本直接做向量化，检索时"语义焦点"会被稀释（一句话被淹没在整段里）；切成小片召回更准，但小片可能截断上下文。父子分块解决这对矛盾：**子块（child）小而聚焦，用于精确检索；父块（parent）大而完整，用于提供上下文**。

**项目实现**：
- 子块：约 220 字，重叠 40 字（重叠防止关键句恰好被切在边界）；
- 父块：段落分组约 600 字（上限 900），以自然段落为边界聚合子块；
- 检索时用子块匹配，命中的子块通过 `small_to_big` 映射到父块，回答基于父块内容。

**代码位置**：
- `backend/app/rag/splitter.py`：`build_parent_child_splitters()`、`ParagraphGroupTextSplitter`
- 聚合：`backend/app/rag/retriever.py`：`small_to_big()`

**关键代码**（splitter.py 参数）：

```python
def build_parent_child_splitters(
    parent_chunk_size: int = 600,
    parent_max_chunk_size: int = 900,
    child_chunk_size: int = 220,
    child_overlap: int = 40,
) -> tuple[ParagraphGroupTextSplitter, RecursiveCharacterTextSplitter]:
    parent_splitter = ParagraphGroupTextSplitter(
        target_chunk_size=parent_chunk_size,
        max_chunk_size=parent_max_chunk_size,
    )
    child_splitter = build_text_splitter(
        chunk_size=child_chunk_size,
        chunk_overlap=child_overlap,
    )
    return parent_splitter, child_splitter   # 返回 (父块切分器, 子块切分器)
```

---

## 5. 向量化与知识库构建（本地 BGE + OpenSearch）

**原理**：Embedding 模型把文本映射为高维向量，语义相近的文本向量距离近；向量库负责存储向量并提供近邻检索（kNN）。

**项目实现**：
- 本地 `bge-base-zh-v1.5`（中文优化，768 维），CUDA + fp16，批量嵌入（batch_size 128）；GPU 出错自动降级 CPU 并冷却探测恢复；
- 向量库 OpenSearch：创建 kNN 索引（HNSW 图），`bulk_index` 批量写入；文档向量 + 原文 + metadata 同库存储；
- 索引账本（`opensearch_meta/manifest.json`）记录每个文件的状态与指纹，支撑增量更新。

**代码位置**：
- `backend/app/rag/embeddings.py`：`BGEEmbeddings`（本地）/ `APIBGEEmbeddings`（可选 API 模式）
- `backend/app/rag/store.py`：`OpenSearchStore.create_index()`、`bulk_index()`、`search_vector()`、`search_bm25()`

**关键代码**（store.py 建索引）：

```python
def create_index(self) -> None:
    body = {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                "vector": {"type": "knn_vector", "dimension": 768, "method": {"name": "hnsw"}},
                "content": {"type": "text", "analyzer": "ik_max_word"},
            }
        },
    }
```

---

## 6. 混合检索与重排序

**原理**：
- **BM25（关键词）**：对专有名词、编号、书名这类精确匹配强；
- **kNN（语义向量）**：对同义改写、口语表述强（"这本书讲了什么" ≈ "内容概要"）；
- 两者互补，混合召回后融合排序；
- **RRF 融合**：两套检索的分数量纲不可比，直接加权难调；RRF 只看排名，按 `1/(k + rank)` 计分相加，鲁棒且无需调权重；
- **reranker（重排）**：召回是"粗筛"（双塔，query/文档分别编码，交互浅）；reranker 是交叉编码器（query+文档拼接深度交互），更准但算力高，只对候选精排。

**项目实现**：召回各 Top 40 → RRF 融合 Top 24 → Small-to-Big 聚合 → 本地 `bge-reranker-v2-m3` 精排 Top 4。

**代码位置**：
- 混合检索：`backend/app/rag/retriever.py`：`hybrid_search()`、`merge_query_results()`（RRF）、`small_to_big()`
- 重排：`backend/app/rag/reranker.py`：`LocalReranker` / `APIReranker`

**关键代码**（retriever.py：kNN + BM25 两路 RRF 融合，取前 24）：

```python
RRF_K = 60  # RRF 平滑常数：排名越靠前，融合分越高

def hybrid_search(store, embeddings, question,
                  recall_k: int = 40, candidate_pool: int = 24) -> list[Document]:
    query_vector = embeddings.embed_query(question)
    dense_docs = store.search_vector(query_vector, recall_k)   # kNN 语义召回 40
    sparse_docs = store.search_bm25(question, recall_k)        # BM25 关键词召回 40
    merged: dict[str, dict] = {}
    for docs in (dense_docs, sparse_docs):
        for rank, doc in enumerate(docs, 1):
            item = merged.setdefault(doc.page_content, {"doc": doc, "rrf": 0.0})
            item["rrf"] += 1.0 / (RRF_K + rank)                # RRF：只看排名
    ranked = sorted(merged.values(), key=lambda item: item["rrf"], reverse=True)
    return [item["doc"] for item in ranked[:candidate_pool]]   # 融合取前 24
```

> 注：查询扩展产生多个改写查询时，每个查询内部已做 kNN+BM25 的 RRF，
> 各查询结果再按"查询内排名"做二次融合（`merge_query_results`），让多个角度的
> 查询都能贡献候选，同时抑制单个查询的噪声。

---

## 7. 查询扩展（Multi-Query / HyDE / 多轮补全）

**原理**：用户问题与文档表述经常"对不上"（口语 vs 书面、指代、多角度）。查询扩展生成多个查询从不同角度检索，提升召回覆盖率：
- **Multi-Query**：把一个问题改写为 N 个独立查询（原意复述、术语表达、同义补充）；
- **HyDE（假设文档）**：让模型先写一段"文档风格"的假设性回答，再用它做向量检索——假设文档在语义上更接近真实文档，搭起"问题→文档"的语义桥梁；
- **多轮补全**：结合历史把"它""这篇"等指代消解为具体篇名；
- **简单问题跳过**：短问题、无复杂词、无指代时直接走原始查询，避免无谓的 LLM 调用（省成本降延迟）。

**项目实现**：三路并行生成 + 同问题 10 分钟缓存（同样的输入产出同样的查询，可复现）。

**代码位置**：`backend/app/rag/query_expander.py`：`QueryExpander.expand()`、`_is_simple()`（简单问题判定）、`_cache_key()`；`backend/app/rag/llm.py`：`DeepSeekChat.rewrite_queries()` / `hypothetical_document()` / `disambiguate()`

**关键代码**（llm.py HyDE 提示词）：

```python
HYDE_TEMPLATE = """请针对用户的问题，写一段 100~200 字的假设性知识库文档片段。
要求：
1. 内容应与真实知识库的风格一致，直接陈述可能回答该问题的内容；
2. 包含可能出现的专有名词、篇名、术语和关键表述；
3. 只输出文档片段本身，不要解释、不要引号。"""
```

---

## 8. 生成与来源编号引用

**原理**：LLM 幻觉是 RAG 的核心风险。约束手段：① 系统提示词强制"回答基于检索内容、引用标注 [n]、不编造编号"；② 检索结果带全局编号，编号与来源卡片一一对应；③ 后处理兜底。

**项目实现**：
- 每条检索结果分配全局 `index`（counter 递增），`extract_sources` 从工具结果提取 sources（含 index/标题/URL）；
- 回答中模型标注 [n]；finalize 阶段按 index 去重（`[n]` 编号与来源卡片一一对应）；
- **附录兜底**：若最终回答完全没标 [n] 且存在联网来源，自动在文末补"参考来源"链接列表。

**代码位置**：
- 编号分配：`backend/app/agent/tools.py`（`counter` 递增、`extract_sources()`）
- 去重/附录：`backend/app/agent/langgraph_agent.py`（`_finalize_node`）

**关键代码**（finalize 去重 + 附录）：

```python
deduped, seen = [], set()
for item in sources:
    key = str(item.get("index") or item.get("content", ""))
    if key and key not in seen:
        seen.add(key); deduped.append(item)
if final_text and not re.search(r"\[\d{1,3}\]", final_text):
    # 模型漏标编号时，自动补“参考来源”附录
    ...
```

---

## 9. CRAG 联网兜底

**原理**：知识库覆盖不足时，允许补充联网结果，但不是无脑兜底——**相关度门控 + 开关门控**：检索为空或最高分低于阈值才触发，且仅当用户本轮开启联网开关；纯知识库模式不静默联网（避免每次查询被拖慢）。

**项目实现**：`best = max(score)`，`best < crag_min_score(0.45)` 且 `web_provider != "off"` 且 `allow_web_fallback` 时，调 `execute_web_search`（DuckDuckGo / Tavily / SearXNG 可插拔），联网结果标记 `type: "web"` 附在回答后面。

**代码位置**：`backend/app/agent/tools.py`：`make_knowledge_base_tool()` 内的 CRAG 判断；`execute_web_search()`

**关键代码**：

```python
if (
    crag_enabled and allow_web_fallback and web_provider != "off"
    and (not result.get("results") or best < crag_min_score)
):
    web = execute_web_search(query, web_provider, ...)
    fallback = web.get("results", [])
    for item in fallback:
        counter[0] += 1
        item["index"] = counter[0]; item["type"] = "web"
    result["fallback_web"] = fallback
```

---

## 10. RAGAS 评估与回归门槛

**原理**：RAG 质量不能只靠感觉。RAGAS 是一套检索+生成评估指标，核心是 **faithfulness（忠实度）**：把回答拆成多个论断，逐个判断是否能在检索上下文中找到依据，由裁判模型（LLM）打分——回答"没依据的编造"越少，faithfulness 越高。

**项目实现**：
- `backend/eval_ragas.py`：对知识库题目逐题调用检索问答，用 DeepSeek 裁判模型计算 faithfulness，结果写入 `ragas_baseline.jsonl`（基线示例：faithfulness ≈ 0.975）；
- 本地评测 `evaluate_agent.py` + `eval_questions.json`（14 题种子集）：记录延迟/工具/计划/token/状态，改动前后对比发现回归；
- `scripts/regression_gate.py`：检索/提示词改动后手动跑的回归门槛。

**代码位置**：`backend/eval_ragas.py`（`load_judge` / `rag_chat`）、`backend/evaluate_agent.py`、`scripts/regression_gate.py`

---

## 11. Agent 编排：LangGraph 状态图

**原理**：手写 ReAct 循环的痛点：达到递归上限直接抛异常、状态管理散落、难以扩展。LangGraph 把编排建模为**显式状态图**——节点 + 条件路由，每个节点返回什么、什么条件走哪条边一目了然，可观测可测试可扩展。

**项目实现**（核心图结构）：

```text
                        START
                          │
                          ▼
                     ┌─────────┐
                     │ prepare │  会话/历史/记忆/计划/工具/消息链组装
                     └────┬────┘
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
      ┌──────────────┐        ┌──────────────┐
      │   dispatch   │        │    agent     │  模型生成，决定是否调工具
      │ 拆解工具型子任务│        └──────┬───────┘
      └──────┬───────┘               │
             ▼                       │ 有 tool_calls
      ┌──────────────┐               ▼
      │  subagent×N  │        ┌──────────────┐
      │  并行执行     │        │    tools     │  执行工具+回填+进度
      └──────┬───────┘        └──────┬───────┘
             ▼                       │
      ┌──────────────┐               │ 无工具调用
      │    merge     │───────────────┤
      └──────────────┘               ▼
                                     │
                              ┌──────────────┐
                              │  finalize    │  去重/持久化/运行记录/trace
                              └──────┬───────┘
                                     ▼
                                    END
```

- **prepare**：组装消息链（见 §18）、规划、任务清单播种、工具列表构建（含 tools 哈希监测）；
- **agent**：`bind_tools(tools)` 流式生成，有 `tool_calls` → tools 节点，否则收尾；工具调用上限（默认 6，任务模式 24）与强制收尾；
- **tools**：执行工具、HITL 审批门（§14）、失败不烧预算（成功才累计，连续失败上限 3）、回填计划进度；
- **finalize**：来源去重、消息持久化（§18 纯追加链落库）、运行记录 + trace、标题后置。

**代码位置**：`backend/app/agent/langgraph_agent.py`：`build_agent_graph()`、`_prepare_node()`、`_agent_node()`、`_tools_node()`、`_finalize_node()`、`_route_after_agent()` 等路由函数

**关键代码**（图构建）：

```python
def build_agent_graph(checkpointer=None):
    graph = StateGraph(AgentState)
    graph.add_node("prepare", _timed_node("prepare", _prepare_node))
    graph.add_node("dispatch", _timed_node("dispatch", _dispatch_node))
    graph.add_node("subagent", _timed_node("subagent", _subagent_node))
    graph.add_node("merge", _timed_node("merge", _merge_node))
    graph.add_node("agent", _timed_node("agent", _agent_node))
    graph.add_node("tools", _timed_node("tools", _tools_node))
    graph.add_node("finalize", _timed_node("finalize", _finalize_node))
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", _route_after_prepare, {"dispatch": "dispatch", "agent": "agent"})
    graph.add_conditional_edges("agent", _route_after_agent, {"tools": "tools", "agent": "agent", "finalize": "finalize"})
    graph.add_conditional_edges("tools", _route_after_tools, {"agent": "agent", "finalize": "finalize"})
    return graph.compile(checkpointer=checkpointer)
```

---

## 12. 子任务分解（Send 并行）

**原理**：计划中有多个"工具型步骤"时，串行执行浪费轮次。LangGraph `Send` 可以把每个步骤派给**独立上下文的子代理**并行执行，各分支返回结论摘要后由 merge 节点汇总——主 Agent 只做拆解与汇总，避免重复检索。

**项目实现**：`_dispatch_node` 把工具型步骤拆成子任务 → `Send` 扇出到 `subagent` 节点（每分支独立 messages + 受限工具集，敏感操作仍走 HITL，最多 2 轮）→ `merge` 合并来源与轨迹、同步任务清单、重排全局引用编号。

**代码位置**：`backend/app/agent/langgraph_agent.py`：`_build_subagent_tasks()`、`_dispatch_tasks()`、`_subagent_node()`、`_merge_node()`、`_renumber_subagent_sources()`

---

## 13. 任务清单硬约束

**原理**：Agent 容易"答非所问"——计划列了 5 步只做 1 步就提前总结。硬约束在**路由层**强制（不是提示词软约束）：清单里还有需要工具的未完成步骤时，模型不允许提前输出最终回答。

**项目实现**：
- 规划后自动播种任务清单（MySQL 持久化、跨轮跟踪），模型通过 `todo_update` 工具维护（list/add/complete/remove/set）；
- 每轮工具执行后按进度自动勾选工具型步骤；纯推理/总结步骤在收尾时自动补完成，未完成的工具型步骤保留未勾选（审计留痕）；
- 路由 `_route_after_agent`：存在未完成工具型步骤 → 推回 agent 继续执行（最多提示 3 次），超限或达调用上限才强制收尾。

**代码位置**：`backend/app/todos.py`（`refresh_todos_for_plan` / `plan_progress` / `make_todo_tool`）、`backend/app/agent/langgraph_agent.py`（`_route_after_agent` 中的硬约束分支）

**关键代码**（路由推回）：

```python
if remaining_hint:   # 还有需要工具的未完成步骤
    force_continue = True
    state["plan_push_count"] += 1
    messages.append(SystemMessage(
        content="【计划硬约束】以下步骤尚未完成且需要工具：..."
        "请继续调用对应工具完成这些步骤，不要提前输出最终回答；"
        "若某步确实无需执行，先用 todo_update remove 删除它..."))
```

---

## 14. 敏感操作人工审批（HITL）

**原理**：写文件/编辑/删除/执行命令是高风险操作，不能让模型自主执行。审批流：工具请求 → 后端推送审批事件 → 用户批准/拒绝 → 决定回传模型继续执行。

**项目实现**：
- 敏感工具（write_file/edit_file/delete_file/bash）默认 `ask` 模式：调用前先请求审批，SSE 推送 `permission_request`，前端渲染审批卡片（命令/路径/内容预览），用户批准/拒绝（可附备注）；
- 阻塞等待期间 SSE 每 15s 心跳保活；超时（默认 300s）自动取消；
- 拒绝原因回传给模型，模型向用户解释影响并给出替代方案；
- 快捷审批：数字键 1/2/3 选择；"本会话记住"与"永久记住"（命令白名单）可减少重复确认。

**代码位置**：`backend/app/permissions.py`（`PermissionManager`：submit/wait/resolve）、`backend/app/agent/langgraph_agent.py`（`_tools_node` 审批门）、`backend/app/api/agent.py`（`/agent/permission/{id}/resolve`）

---

## 15. 断点恢复（checkpoint）

**原理**：长任务可能中断（断电/进程退出/用户取消）。每轮工具执行后把消息链与进度持久化，下次提问检测到待恢复快照就从断点继续，不重复已完成步骤。

**项目实现**：
- 自定义 SQLite checkpoint（`opensearch_meta/checkpoints.db`）：每轮工具执行后保存 messages/sources/tool_trace/plan/todos 等；
- prepare 阶段检测 pending 快照 → 恢复消息链并追加"这是一次中断后恢复的任务…"提示，继续执行；
- finalize 成功后清除快照；
- 另有 LangGraph 原生 checkpointer（时间线审计 + 回滚 API）与消息级回退（rewind）。

**代码位置**：`backend/app/checkpoint.py`（`CheckpointStore`）、`backend/app/native_checkpoint.py`、`backend/app/api/conversations.py`（rewind）、`backend/app/api/advanced.py`（rollback）

---

## 16. MCP 接入（Playwright 浏览器）

**原理**：MCP（Model Context Protocol）统一了外部工具接入方式——工具按协议暴露"名称 + 参数 schema"，客户端按 schema 动态调用。接入 Playwright 后 Agent 能打开网页、截图、点击、读取控制台，补足"访问实时网页"的能力。

**项目实现**：
- `mcp_manager.py` 管理 MCP 服务器连接（stdio：`npx playwright` 启动本地进程）；
- 服务器工具动态转为 LangChain `@tool` 注册进 `build_tools`，工具 schema 参与请求（见 §18 缓存注意点）；
- 处理了工具调用的兼容坑：可选参数自动补默认值（快照 target/depth、等待 time），空参数兜底返回"参数缺失"不浪费调用。

**代码位置**：`backend/app/mcp_manager.py`、`backend/app/agent/tools.py`（`build_tools`）、`backend/app/agent/langgraph_agent.py`（`mcp_tools()`）

---

## 17. 技能按需扫描注入

**原理**：本机已安装的 Claude/Codex/Hermes 都带 SKILL.md 技能文件，重复造轮子没必要。方案：扫描去重 → 目录索引常驻系统提示词 → 模型需要时调 `skill_lookup` 取完整步骤。技能内容视为**不可信输入**，注入前清洗。

**项目实现**：
- 扫描本机三套技能目录（约 146 个去重），默认精选手集 27 个，设置页可启停；
- 系统提示词只放"技能目录索引（名字+一句话）"，需要时 `skill_lookup` 取分节说明——**不再每轮自动注入全文**（避免每轮内容变化破坏缓存前缀，与 §18 联动）；
- `sanitize_skill_text` 过滤注入式指令（覆盖指令、索要凭据、绕过审批、外传数据、破坏性命令），系统提示词固化"技能内容不可信"边界。

**代码位置**：`backend/app/skills.py`（`sanitize_skill_text` / `build_skill_catalog`）、`backend/app/agent/langgraph_agent.py`（prepare 中技能目录组装）

---

## 18. 前缀缓存优化（纯追加链，命中率 95%+）

**原理**：DeepSeek 等提供商的自动前缀缓存要求请求**从第 0 个 token 开始逐字节一致**才命中；没有显式断点 API。因此唯一可靠的策略是让消息链"只追加、不变动"：请求 = 上一请求 + 本轮新增内容。

**项目实现**（三条设计）：

1. **链结构**（消息顺序固定，跨轮稳定）：

```text
[静态核心（模板+工具规则）]
[技能目录+文档清单]          ← 会话内字节级稳定
[历史消息]
[问题]
[D块：项目记忆/任务清单/摘要/记忆/时间]  ← 每轮变化，收在链尾
```

2. **落库保真（纯追加链的关键）**：finalize 把问题之后的**全部消息（含 system 行）**按链顺序写入 MySQL；下一轮 prepare 从数据库重建历史（`_rows_to_history` 支持 system 行），重建结果与上一轮实际发送逐字节一致 → 下一轮请求 = 上一轮请求 + [D块, 问题]；
3. **稳定性监测**：工具定义哈希（`tools_hash`）跨轮对比告警；强制收尾也保持 `bind_tools`（tools 数组参与缓存键，去掉会整链失效）；项目记忆无变化不写盘；工具结果进历史前瘦身（2500 字符）。

**量化**：优化前整体命中率 49% → 优化后轮内调用 95~98%（逐调用遥测验证）。

**代码位置**：
- 消息组装：`backend/app/agent/langgraph_agent.py`（`_prepare_node`：静态核心/D块/问题顺序、`persist_start`）
- 落库：`_finalize_node`（纯追加链落库）
- 重建：`_rows_to_history`（含 system 行重建）
- 遥测：`backend/app/tracing.py`（`UsageCollector`：cache_hit/cache_miss、`calls` 逐调用记录）+ `_agent_node`（`runtime["calls"]`）
- 工具哈希：`_tools_prefix_hash()`

**关键代码**（prepare 链结构）：

```python
messages.append(SystemMessage(content=system_prompt_core))   # 1. 静态核心
if static_dynamic_text:
    messages.append(SystemMessage(content=static_dynamic_text))  # 2. 技能/文档
messages.extend(history_messages)                             # 3. 历史（DB 重建）
persist_start = len(messages)
# 4. D 块（每轮变化）收在问题之后，变化只影响链尾
messages.append(SystemMessage(content=time_context))
messages.append(HumanMessage(content=question))
```

**关键代码**（逐调用遥测，`_agent_node`）：

```python
usage = getattr(merged, "usage_metadata", None)
if usage:
    runtime.setdefault("calls", []).append({
        "in": usage.get("input_tokens"),
        "read": (usage.get("input_token_details") or {}).get("cache_read"),
        "out": usage.get("output_tokens"),
    })
```

---

## 19. 工程化：测试 / 可观测 / 前端

**测试**：
- 121 个 pytest 用例：任务清单、文件工具安全边界与命令白名单、CUDA 降级、技能清洗、联网可信度、CLI 交互、取消机制、hooks 门禁、文档文件服务、写后验证等；
- pre-commit 钩子：`git commit` 前自动跑 pytest，失败阻止提交（`--no-verify` 可跳过）。

**可观测**：
- `agent_runs` 表：每次运行的 question/plan/tool_trace（含耗时）/answer_len/latency/status/**token 用量（含逐调用缓存遥测 calls）**/分阶段耗时 timings/工具哈希；
- JSONL trace（`opensearch_meta/traces/YYYY-MM-DD.jsonl`）：线程级 token 聚合；
- `/api/metrics`：请求量、错误率、检索/生成/扩展平均耗时。

**前端**：Vue 3 + Element Plus：打字机流式（28ms 节流 + 积压加速）、引用索引可点击溯源、执行计划/任务清单/工具轨迹/审批卡、主题切换、设置面板（多供应商/思考强度/技能管理）。

**代码位置**：`backend/tests/`、`backend/app/tracing.py`、`backend/app/api/runs.py`、`frontend/src/views/ChatView.vue`、`frontend/src/App.vue`

---

## 20. 面试一句话总结（每个章节的"30 秒版"）

| 技术点 | 一句话 |
|---|---|
| 父子分块 | 子块精准召回、父块完整上下文，解决"召回准"与"上下文全"的矛盾 |
| 混合检索 | BM25 管精确匹配、向量管语义改写，RRF 按排名融合避开量纲问题 |
| reranker | 交叉编码器深度交互，比双塔粗筛更准，只对候选精排控制成本 |
| HyDE | 把问题转成"文档形态"再检索，搭起问题与文档的语义桥梁 |
| CRAG | 相关度门控 + 开关门控的联网兜底，不静默拖慢纯知识库模式 |
| RAGAS | faithfulness 判断回答是否"有据可依"，基线 0.975 + 回归门槛 |
| LangGraph | 显式状态图：节点 + 条件路由，可观测可测试可扩展 |
| 硬约束 | 路由层强制"未完成不总结"，不是提示词软约束 |
| HITL | 审批事件 → 用户决定 → 拒绝原因回传模型，超时自动取消 |
| checkpoint | 每轮落盘消息链，中断后从断点继续，不重复已完成步骤 |
| MCP | 工具标准化协议，schema 驱动调用，接 Playwright 补网页能力 |
| 技能注入 | 目录索引常驻 + 按需取全文 + 防注入清洗，且不破坏缓存前缀 |
| 前缀缓存 | 静态核心 + 纯追加链 + 落库保真，轮内命中 95~98% |
