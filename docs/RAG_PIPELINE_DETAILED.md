# RAG 管线详解(入库 → 检索 → 生成全链路)

> 依据 `backend/app/rag/` 全部源码逐行核对:`loader.py`(382 行)/
> `splitter.py`(112)/`embeddings.py`(237)/`store.py`(190)/`retriever.py`(140)/
> `query_expander.py`(151)/`reranker.py`(220)/`service.py`(578)/`llm.py`(234)/
> `utils.py`/`vision.py`。适合面试深挖、调检索参数、排查检索质量问题。

---

## 1. 全链路总览

### 1.1 入库(离线,可增量)

```text
data/ 文件(8 格式)
  → loader.load_documents    按扩展名分发解析器(布局 PDF / EPUB 章节 / .doc 双引擎…)
  → splitter 父子切分        parent(段落分组~600字) + child(递归小窗~320字,重叠 64)
  → embeddings 本地嵌入       BGE-base-zh-v1.5,768 维,fp16(或 API 嵌入)
  → store.bulk_index         写入 OpenSearch(原文+分词+向量同库,_id=内容哈希)
  → manifest/chunks 账本      指纹记录 + parent 副本落盘(增量索引依据)
```

### 1.2 在线检索(一次问答)

```text
question
  → query_expander.expand        Multi-Query / HyDE / 多轮补全(并行,简单问题跳过)
  → hybrid_search ×N             每路查询:kNN 向量 + BM25 关键词,RKF RRF 融合
  → merge_query_results          跨查询合并(2026-08 起按 source 轮流取候选)
  → small_to_big                 child 召回 → parent 全文聚合(最多 10 个)
  → reranker.rerank              本地 bge-reranker-v2-m3 精排 Top 6
  → llm.generate                 基于上下文生成(带来源引用)
```

---

## 2. 文档加载(loader.py)

### 2.1 支持格式与解析器

| 格式 | 解析器 | 要点 |
|---|---|---|
| txt / md / markdown | TextLoader | utf-8,autodetect_encoding |
| csv | CSVLoader | utf-8 |
| docx | Docx2txtLoader | |
| pdf | PyMuPDF4LLM 布局感知 | 见 2.2 |
| epub | ebooklib + BeautifulSoup | EPUB 本质是 ZIP(XHTML 章节),按章节分组,章节名入 metadata |
| doc | doc2txt(内置 antiword)→ 回退 legacy-doc | 双引擎,失败报"无法解析" |
| xlsx | openpyxl(延迟导入) | 每 sheet 一个 Document,每行单元格用 `\|` 连接 |

统一补充 `relative_path` 元数据(子目录重名文档不混淆,来源定位精确到文件)。

### 2.2 PDF 解析策略(2026-08 优化)
1. **布局感知优先**:`pymupdf4llm.to_markdown(page_chunks=True)` 保留标题层级/
   段落/表格/页码,页面阅读顺序合理 → 显著改善按段落分组的切分质量;
2. **并行兜底**:大 PDF(≥300 页)且配置 `pdf_parse_workers>0` 时按页范围
   多进程解析(布局推理是进程内全局状态,线程并行无效);有段失败回退串行;
3. **失败回退**:PyMuPDF4LLM 抛错 → PyPDFLoader(纯文本按页);
4. **扫描页 OCR**(默认关,`PDF_VISION_OCR_ENABLED`):文本 <30 字的页判定为
   扫描件,用 SenseNova 视觉模型 OCR,单文件上限 `PDF_VISION_OCR_MAX_PAGES=20`,
   输出标记 `[扫描页 OCR]`,metadata 带 `ocr: True`。

### 2.3 解析容错
- 单个文件解析失败 → 打印"跳过 文件名:原因",**不阻断其他文件**;
- ImportError → 提示缺少哪个依赖。

---

## 3. 切分(splitter.py)

### 3.1 为什么父子双块(基于真实语料的切分实验)
- 纯递归切分只有 **8%** 块落在句子边界,语义常被切断;
- 按段落分组切分约 **47%** 落在句子边界;
- 结论:child 用递归小窗口(召回精度),parent 用段落分组(上下文完整)。

### 3.2 参数(默认)
| 块 | 大小 | 重叠 | 切分器 |
|---|---|---|---|
| child | 320 字 | 64 | `RecursiveCharacterTextSplitter`,分隔符优先级:`\n\n → \n → 。 → ！ → ？ → ； → ， → 空格 → ""` |
| parent | 600 字(目标) | - | `ParagraphGroupTextSplitter` |

### 3.3 ParagraphGroupTextSplitter 算法
1. 按 `\n\s*\n` 切段落;
2. 多个短段落合并到接近 600 字(块间 `\n\n` 连接,保留段落边界);
3. 单段 >900 字:先按句子边界切(`(?<=[。！？；])`),个别超长句子最后硬切;
4. parent 的 `parent_id` = parent 文本的 MD5(与 OpenSearch `_id` 同源);
   child metadata 带 `parent_id` + `parent_content`(聚合时取全文)。

---

## 4. 嵌入(embeddings.py + utils.py)

### 4.1 双 provider(运行时切换,设置页生效)
| provider | 实现 | 说明 |
|---|---|---|
| local(默认) | `LocalBGEEmbeddings` | `bge-base-zh-v1.5`,768 维;CUDA 下默认 fp16(实测提速 ~3 倍);查询加 BGE 官方前缀"为这个句子生成表示以用于检索相关文档：",归一化 |
| api | `APIBGEEmbeddings` | OpenAI 兼容 `/v1/embeddings`(SiliconFlow bge-m3 等);批量按 max_batch 分片,按 index 排序保证顺序;维度首次调用探测 |

### 4.2 模型懒加载
- `LocalBGEEmbeddings._ensure_model` 首次 encode 才加载(启动不占显存);
- torch/sentence-transformers 仅在本地路径导入(lite 镜像不装这两个包);
- `utils.py` torch 为可选导入,无 torch 时 `detect_device` 恒返 cpu。

### 4.3 CUDA 容错
- 推理异常且 `is_cuda_error`(OutOfMemoryError 或消息含 cuda+error 特征)→
  释放 GPU 模型 → 降级 CPU 完成本次检索 → `CUDA_RETRY_AFTER=300s` 冷却后
  `_maybe_retry_cuda` 用小探针(`torch.zeros` 实际 kernel)探测恢复 → 切回 GPU;
- 显卡瞬时故障不会让知识库查询持续失败。

### 4.4 维度动态适配
- `OpenSearchStore(dimension=self.embeddings.dimension)`:索引维度跟随当前
  嵌入模型(本地 768 / bge-m3 1024);**切换模型后需重建索引**(维度冲突)。

---

## 5. 索引(store.py + service.py 账本)

### 5.1 OpenSearch 索引结构(`create_index`)

```yaml
settings:
  index: { knn: true, number_of_shards: 1, number_of_replicas: 0 }
  analysis:
    analyzer cn_word: whitespace 分词器 + lowercase 过滤器
mappings:
  content:        text            # 原文(不索引)
  content_seg:    text + cn_word  # jieba 分词结果,空格连接 → 词级 BM25
  metadata:       object, enabled: false   # 元数据只存不索引
  metadata_source: keyword        # 来源文件名(过滤/聚合用)
  metadata_page:  integer         # 页码
  vector:         knn_vector, dimension=动态, HNSW, cosinesimil, lucene
```

> **为什么 content_seg 与 content 分开**:OpenSearch 默认分词器对中文不友好,
> 项目先 jieba 切词、空格连接、whitespace 分词 → 实现"词级 BM25"。

### 5.2 写入(`bulk_index`)
- `_id` = 内容 MD5:同一段文本重复写入覆盖而非堆积(增量索引的基石);
- 批量 64/128,`/_bulk` ndjson;`result.errors` 时抛错(附前 3 条明细);
- 写完 `_refresh` 保证可查;
- 检索接口直接 REST(httpx),不依赖 opensearch-py。

### 5.3 文件指纹账本(增量索引的核心,service.py)
- `_compute_file_fingerprints`:每文件指纹 = SHA-256(大小 + 修改时间纳秒);
- `manifest.json`:`{files: {相对路径: 指纹}, parent_count, child_count}`;
- `chunks.json`:parent 副本(增量追加时复用,不重新切分旧文件);

### 5.4 ensure_index 三分支(307 行)
```text
① 数据没变 + 索引存在 + 文档数>0  → 直接复用(命中索引缓存)
② 只新增文件                      → 只加载/切分/嵌入新文件,追加写入,
                                    账本合并旧 parents + 新 parents
③ 修改/删除/强制重建              → 删索引 → 重建 → 全量加载嵌入写入
```
- 删除文件后 `delete_document` 强制全量重建(旧向量必须清);
- 上传文件/`add_document` 走增量路径。

---

## 6. 查询扩展(query_expander.py + llm.py)

### 6.1 三策略(并行,`ThreadPoolExecutor(max_workers=3)`)

| 策略 | 模板要点 | 作用 |
|---|---|---|
| Multi-Query | 改写 N=3 个独立查询:原意复述/篇名术语/同义补充;名句必须原样写入(如"实事求是") | 覆盖多角度表述 |
| HyDE | 写 100~200 字"假设性知识库文档片段",风格贴近真实文档 | 搭起"问题→文档"语义桥梁 |
| 多轮补全 | 结合历史把"它/这篇/刚才说的"补全为独立问题 | 消解指代 |

- 温度 0 + **关闭思考**(`_rewrite_llm`):同样的输入产出同样的改写,检索可复现;
- 每策略失败独立降级为原问题;
- **简单问题跳过**(`_is_simple`):≤16 字 且 无复杂词(总结/分析/对比/原理/如何…)
  且无指代词(它/这/那/该/此…) → 只走原查询(省一次 LLM 调用);
  **含《》书名号的书名/篇名题强制扩展**(2026-08,原查询常与库内表述错位);
- **10 分钟缓存**:键 = 问题[:200] + 最近一条历史后 60 字符;缓存 >128 条清空;
- 输出上限 `MAX_QUERIES=4`,原问题永远第一,去重。

### 6.2 查询扩展在 Agent 工具里怎么被调用
`knowledge_base_search` 工具直接走 `RAGService.retrieve`(含扩展+检索+精排全管道);
纯对话/历史问答接口 `/api/chat` 走 `ask`(同管道 + 生成)。

---

## 7. 混合检索与两级融合(retriever.py)

### 7.1 每路查询的混合检索(`hybrid_search`)
```text
query_vector = embed_query(question)
dense  = store.search_vector(query_vector, recall_k=60)   # kNN 语义
sparse = store.search_bm25(jieba分词后, recall_k=60)      # BM25 关键词
RRF: score(doc) = Σ 1/(RRF_K + rank), RRF_K=60
按 rrf 降序取 candidate_pool=32 → metadata.hybrid_score
```

### 7.2 跨查询合并(`merge_query_results`)
- 每个扩展查询内部已做过 kNN+BM25 RRF,这里再融合一次:多个角度的查询都能
  贡献候选,同时抑制单查询噪声;取前 40;
- **2026-08 source 保底分流**:融合后按来源(source)分组轮流取候选,保证每本书
  必进候选池——打破同书多格式(同一本书的 epub+pdf)垄断,多跳题第二篇文档的内容
  不再被挤出(单源题退化为纯 RRF 序)。

### 7.3 Parent-Child 聚合(`small_to_big`)
- 按 `parent_id` 分组,每组保留相关性最高 child 的分数;
- 返回 parent **全文**(`parent_content`)作为上下文,最多 `MAX_PARENTS=10`;
- 旧索引无 parent 字段 → 按 child 内容本身去重兜底。

### 7.4 精排(`reranker.py`)
- `LocalReranker`(CrossEncoder bge-reranker-v2-m3,模型懒加载一次)或
  `APIReranker`(OpenAI 兼容 `/v1/rerank`,兼容 SiliconFlow/Jina/Cohere 格式);
- query × 候选逐对打分(batch 8),取 Top `RERANK_TOP_K=6`,
  `metadata.rerank_score` 落库(供前端展示分数与排序);
- API rerank 未打分候选按原顺序附尾(保持返回数稳定);
- 为什么召回是双塔/精排是交叉编码:双塔粗筛便宜(每路候选 32→合并 40),
  交叉编码深度交互更准但贵,只对少量候选跑。

---

## 8. CRAG 联网兜底(tools.py)

`knowledge_base_search` 工具内:
```python
if (crag_enabled and allow_web_fallback and web_provider != "off"
    and (无结果 or best < crag_min_score=0.45)):
    → 联网搜索,结果带 index/type="web" 并入 fallback_web
```
- `allow_web_fallback = use_web_search`(用户本轮开联网才兜底);
- **纯知识库模式不再静默爬网**(实测 KB 单题 17~25s,此前 50~218s)。

---

## 9. 生成(llm.py)

### 9.1 SYSTEM_TEMPLATE(回答纪律)
- 仅以文本内容为唯一事实依据;没有的信息明确回答"抱歉,提供的文本中没有
  这个信息",绝不编造;
- 先结论后论据,引用说明出处(文件名/页码);文本与常识冲突以文本为准并提示。

### 9.2 调用
- `DeepSeekChat.generate(question, context, history)`:`system+history+question`
  组装,`_llm`(temperature 0.3,思考模式可配);
- 流式 `astream` 逐 chunk;Agent 场景的生成实际走 agent 节点的 bind_tools 主循环
  (RAG 旧接口 `/api/chat` 才走这里);
- 用量经线程级收集器统计(cache hit/miss)。

---

## 10. 记忆召回与复用(与 RAG 共享 embedding)

- `context.retrieve_memories` 与知识库检索**共用同一嵌入模型与 GPU 锁**;
- 流程:n-gram 粗筛(≤100 候选 + 最近 50 补位)→ embed_query + embed_documents →
  余弦 ≥0.35 → Top3 → 相关性不足最近记忆补位 → token 预算 600 裁剪;
- 事实去重:文本相等或向量相似 >0.95。

---

## 11. 并发与性能

| 项 | 机制 |
|---|---|
| GPU 保护 | `MAX_CONCURRENCY=4` 信号量(检索/重排/记忆召回段);知识库工具调用时 acquire_gpu |
| LLM 保护 | `LLM_MAX_CONCURRENCY=8` 信号量(生成段,不占 GPU 锁) |
| 查询扩展缓存 | 同问题 10 分钟(含历史尾 60 字符语境)——检索层本身无结果缓存,缓存只在扩展层与 per-run 工具层 |
| per-run 工具缓存 | 查询类工具同参重复调用直接命中(见编排文档 §6.2) |
| 索引缓存 | 文件指纹没变直接复用索引(增量三分支) |
| 扩展并行 | Multi-Query/HyDE/多轮补全三路并发(扩展耗时近减半) |
| PDF 大文件 | ≥300 页多进程解析(默认串行,`pdf_parse_workers` 配置) |

---

## 12. 参数速查(全部可环境变量覆盖)

| 参数 | 默认 | 含义 |
|---|---|---|
| PARENT_CHUNK_SIZE / PARENT_MAX_CHUNK_SIZE | 600 / 900 | parent 目标/上限 |
| CHILD_CHUNK_SIZE / CHILD_OVERLAP | 320 / 64 | child 大小/重叠 |
| MAX_PARENTS | 10 | 返回给模型的 parent 数 |
| RECALL_K / CANDIDATE_POOL / RERANK_TOP_K | 60 / 32 / 6 | 召回/融合/精排 |
| QUERY_EXPANSION_ENABLED / EXPANSION_* | 1 | 扩展开关(多路/简单跳过) |
| CRAG_FALLBACK_ENABLED / CRAG_MIN_SCORE | 1 / 0.45 | 联网兜底门控 |
| EMBEDDING_FP16 / EMBED_BATCH_SIZE | 1 / 128 | 精度/批大小 |
| EMBEDDING_PROVIDER / RERANKER_PROVIDER | local | local / api |
| MAX_CONCURRENCY / LLM_MAX_CONCURRENCY | 4 / 8 | 并发闸门 |
| PDF_LAYOUT_ENABLED / PDF_VISION_OCR_ENABLED | 1 / 0 | PDF 布局/扫描 OCR |

---

## 13. 面试一句话

"检索管线:8 格式文档入库(布局感知 PDF + 增量索引,指纹账本只嵌新文件)→
父子切分(child 320 字精确定位、parent 600 字完整上下文,块级去重)→
BGE 本地嵌入(CUDA 容错自动降级 CPU)→ OpenSearch kNN+BM25 同库 →
每路 RRF 融合 → 跨查询合并(source 分流)→ small-to-big 聚 parent →
本地 reranker 精排 Top6 → CRAG 相关度门控兜底联网;查询扩展三路并行
(简单问题跳过、书名号强制扩展)、温度 0 可复现、10 分钟缓存;生成遵守
'无中生有不编造、引用带出处'纪律。"
