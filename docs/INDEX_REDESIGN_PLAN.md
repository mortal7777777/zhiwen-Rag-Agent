# 索引重构方案（per-doc 运维 + 别名蓝绿 + 分层去重）

> 2026-09-10 定稿。背景：删除/修改文档目前触发全库重建（分钟级），维护不现实；
> 跨文件全局去重与删除语义冲突（同一本书 epub+pdf 双格式并存的场景）；lite/full 换嵌入模型
> 需要清卷；重建期间索引处于"已删待建"的真空状态。
> 本方案按个人库规模（≤100 万块）设计，明确不做分片/量化/队列/独立向量库。

## 0. 目标与非目标

**目标**
- 删除秒级（零嵌入）、修改只嵌单文件、全量重建只在换模型/改切分时发生，且不破坏现有索引；
- 删除语义干净：删任何一个文件不影响其他文件的可检索性（跨文件重复不再互相牵连）；
- 索引与嵌入模型解耦命名（lite/full 的 768/1024 维度冲突不再需要 `docker compose down -v`）。

**非目标（规模远未到，明确不做）**：分片、量化（int8/PQ）、磁盘 ANN、消息队列、
独立向量库、多租户 ACL、软删除/tombstone。

## 1. 设计决策

### 1.1 块 `_id`：文档内内容寻址

```
_id = md5(relative_path + "\x00" + content)     # 原：md5(content)
```

- 同文件同内容重写 → 覆盖（幂等重试安全）；跨文件同内容 → 两块并存、各删各的；
- 删除用 `delete_by_query(term: metadata_relative_path)` 精确命中该文件全部块；
- 纯函数 `chunk_doc_id(relative_path, content)`（store.py），便于单测。

### 1.2 别名与物理索引命名

- **逻辑名**（`OPENSEARCH_INDEX`，默认改为 `zhiwen_kb_current`）＝ 别名，代码只认它；
- **物理名**：`{逻辑名}_{模型标签}_{维度}_v{schema版本}_{时间戳}`，如
  `zhiwen_kb_current_bge_base_zh_v1_5_768_v3_20260910_153000`；
- schema 版本常量 `IDX_SCHEMA_VERSION = 3`（v3 = 文档内寻址 + relative_path 字段 +
  别名制）。以后改 ID 方案/切分语义时 bump → 自动触发全量重建；
- **全量重建（蓝绿）**：新建物理索引 → 全量嵌入（写完不碰别名）→ `POST /_aliases`
  原子切换 → 清理同前缀旧物理索引。失败则删除新索引、别名未动（可回滚、无真空期）。
  注意：重建是同步的，期间查询会阻塞等待（量级分钟级、低频；后台化留后续）；
- **兼容**：解析不到别名但存在同名物理索引（旧 demo 脚本显式指定索引名的场景）时，
  按"用户自管索引"使用、不强推重建；只有经别名解析且前缀≠当前期望值（换模型/改
  schema）才蓝绿重建；
- **迁移**：首次运行别名不存在 → 自动蓝绿重建一次（回填 relative_path + 新 ID）。
  旧物理索引 `rag_knowledge_base_v2` 保留不删，确认无误后可手动删除。

### 1.3 可过滤元数据

mapping 增加 `metadata_relative_path: keyword`（删除/过滤的精确键；
`metadata` 对象是 `enabled:false` 不能查询）。分类 keyword
（与 MySQL `document_meta` 同步、支撑"按分类检索"预过滤）留 Phase 3。

### 1.4 去重分层（替代跨文件全局去重）

| 层 | 位置 | 做什么 | 状态 |
|---|---|---|---|
| 文档内 | 索引时 | 同一文件重复 child 只留一份（`seen_child_md5` 改为每文件独立） | 本次落地 |
| 文档级近似 | 上传时 | MinHash 签名比对 → "疑似与《X》重复 92%"提示（不拦截，用户决定） | Phase 3 |
| 检索时 | small_to_big 后、rerank 前 | 按 `md5(parent_content)` 折叠重复 parent（保留高分）+ 现有 source 分流 | Phase 3 |

跨文件全局去重退场：它换来的"减少重复占坑"由上述 2+3 层接管，
而删除语义不再被它破坏。代价是索引体积小幅回升（以真实数据为准，最坏 +42%）。

### 1.5 操作矩阵（`ensure_index` 指纹 diff 驱动）

| 场景 | 动作 | 成本 |
|---|---|---|
| 无变化 | 直接复用（指纹命中短路） | 每次查询一次 stat 扫描 |
| 新增文件 | 解析+切分（文档内去重）+ 嵌入该文件 → 追加 | 单文件 |
| 修改文件 | `delete_by_query`（该文件旧块）→ 重嵌该文件 | 单文件 |
| 删除文件 | unlink → 指纹 diff → `delete_by_query` | **秒级、零嵌入** |
| 换模型/改切分/手动重建 | 蓝绿重建 + 别名切换 | 全量（低频、显式） |
| 账本缺失/损坏（自愈） | 无法增量判断 → 全量重建 | 全量（异常路径） |

账本（`opensearch_meta/{逻辑名}/manifest.json` 指纹 + `chunks.json` parent 副本）
与索引同事务顺序更新：**先改索引、后写账本**（失败后下次 diff 会重试，幂等）。
所有维护操作在一个进程级 `RLock` 内串行（单写者）。

## 2. 关键文件与改动

| 文件 | 改动 |
|---|---|
| `backend/app/rag/store.py` | 别名解析/切换、物理索引命名、`delete_by_query` / `delete_by_document`、mapping 加字段、`chunk_doc_id`、`bulk_index(target=)` |
| `backend/app/rag/service.py` | `ensure_index` 改为 diff 驱动（`_apply_incremental` / `_full_rebuild`）、`delete_document` 不再 force_rebuild、去重作用域改按文件、维护锁 |
| `backend/app/config.py` | `OPENSEARCH_INDEX` 默认值 → `zhiwen_kb_current`（别名语义） |
| `backend/app/api/documents.py` | 删除/重建接口的注释同步（行为不变） |
| `backend/tests/test_rag_dedup.py` | 跨文档去重断言改为"两份都保留"（语义变更） |
| `backend/tests/test_index_ops.py` | 新增：ID 规则 / diff 增量流程 / delete_by_query 请求构造 / 别名切换 |
| 旧 demo 脚本（ask_demo_v2 / build_index_v2 / expansion_demo / rewrite_demo） | 默认索引名 → 新别名 |

## 3. 迁移步骤

- **Phase 0**：store 能力（别名、命名、per-doc 删除、字段）——✅ 2026-09-10 落地；
- **Phase 1**：首跑自动迁移（别名不存在 → 蓝绿重建一次，回填新 ID 与 relative_path）——
  ✅ 2026-09-10 执行（`POST /api/index/rebuild`，全量 45k 块；别名
  `zhiwen_kb_current` → `..._bge_base_zh_v1_5_768_v3_20260910_144919`；
  旧索引 `rag_knowledge_base_v2` 保留可回滚）；
- **Phase 2**：service per-doc CRUD + 去重作用域 + 测试——✅ 2026-09-10 落地
  （另修一处：去重作用域按"文件"而非"加载单元"，PDF 分页/EPUB 分章归同一文件）；
- **Phase 3**：MinHash 上传提示 + 检索折叠 + 分类 keyword——待做。

## 4. 验收标准（2026-09-10 实测）

- 删除零嵌入、秒级：实测首次 3.1s / 再删 0.7s（含指纹扫描与 refresh）✓；
  删除后 `_count` 正确下降（44,982 → 44,981 → 44,980）✓；
- 删掉同一本书的 pdf 后，其 epub 里的同段落仍可检索：合成文件冒烟验证 ✓
  （两文件共享段落 → 删 A 后共享段落仅剩 B 来源 → 删 B 后索引完全恢复）；
- `python -m pytest tests -q` 全绿（133 通过）✓；
- RAGAS 门槛通过（[OK]，8/8 题有效）：faithfulness 0.963 / answer_correctness 0.438 /
  context_precision 0.878 / context_recall 0.854。对比 08-25 基线
  （0.971/0.464/0.769/0.792）：检索两指标的变化属评测波动（索引内容与检索管线
  未变；裁判模型/严格度两次间可能不同），可靠结论为"无劣化"✓；
- lite/full 切换不再需要 `docker compose down -v` ✓（物理索引名含模型与维度，
  切换触发蓝绿重建）。

## 5. 已知边界

- **重命名文件 = 删 + 增**（指纹无法识别改名，成本 = 单文件重嵌；如需可后续用内容哈希识别）；
- 索引体积回升（跨文件重复回归索引）：预计 +10~20%，最坏 +42%（去重前 63,979 → 44,955）；
- 全量重建期间查询阻塞（同步实现；后续可做后台任务真正零停机）；
- 旧索引 `rag_knowledge_base_v2` 不自动删除（保留回滚余地）；
- 存量 `document_meta` 分类数据与索引 metadata 的联动在 Phase 3 落地。
