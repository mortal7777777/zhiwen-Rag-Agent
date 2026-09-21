# OpenSearch 操作手册与全面讲解（本项目专用）

> 面向不了解 OpenSearch 的读者:先讲它是什么、为什么本项目选它,
> 再讲部署、概念、本项目索引结构、日常运维命令和排错。
> 跟着命令走即可;本机环境是 OpenSearch 3.5.0(Windows 原生安装),
> Docker 环境是 2.19.0,命令通用。

---

## 1. OpenSearch 是什么

一句话:**一个既能做关键词搜索、又能做向量相似度搜索的数据库**。

- 它是 **Elasticsearch(ES) 的开源分支**(AWS 于 2021 年从 ES 分出),API 与 ES 几乎兼容
- 底层是 **Lucene**——业界最成熟的全文搜索引擎库(倒排索引 + BM25)
- 本项目同时用它的两大能力:
  - **kNN 向量检索**:存 768 维的文本向量(BGE 嵌入),按余弦相似度找语义相近的文本
  - **BM25 关键词检索**:精确匹配专有名词、编号、书名
- 两者合在一起,就是本项目"混合检索"的地基(README 的 RRF 融合)

### 为什么不用别的?

| 选项 | 缺点(对本项目而言) |
|---|---|
| 纯内存向量库(FAISS/Chroma) | 不持久化要自己管理文件,且没有 BM25 关键词检索 |
| MySQL 存向量 | 向量检索性能差,没有原生 kNN 索引 |
| Elasticsearch | 向量检索要装插件,且商用协议有争议;OpenSearch 开箱即用 |

---

## 2. 本项目两种部署方式

### 2.1 本机原生安装(你目前在用的)

安装目录:`D:\AI\新建文件夹 (3)\opensearch-3.5.0-windows-x64\opensearch-3.5.0`

```powershell
# 启动(前台,窗口别关)
cd D:\AI\新建文件夹 (3)\opensearch-3.5.0-windows-x64\opensearch-3.5.0
.\bin\opensearch.bat

# 验证(另开一个终端)
curl http://localhost:9200
```

看到带版本号的 JSON 即成功。启动时该窗口会持续打日志,**Ctrl+C 停止**。

**配置文件**(`config/opensearch.yml`)本项目用的关键项:

```yaml
network.host: 0.0.0.0        # 监听所有网卡(本机访问用 localhost 也行)
http.port: 9200              # HTTP 端口
discovery.type: single-node  # 单节点模式(不用集群发现,省事)
plugins.security.disabled: true   # 关掉安全插件(测试环境;生产不要这样)
```

> 改了配置需要重启 OpenSearch 才生效。

### 2.2 Docker 方式(随项目一键起)

`docker-compose.yml` 里对应服务:

```yaml
opensearch:
  image: opensearchproject/opensearch:2.19.0
  environment:
    - discovery.type=single-node
    - DISABLE_SECURITY_PLUGIN=true   # 等价于 plugins.security.disabled: true
    - http.port=9200
    - OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m   # JVM 堆内存限制,防吃满内存
```

数据存在 Docker 卷 `opensearch-data` 里,`docker compose down` 不会丢,`down -v` 会清空。

> ⚠️ 踩坑记录:不要同时设置 `DISABLE_SECURITY_PLUGIN=true` 和
> `plugins.security.disabled=true`,两者冲突会导致容器启动退出(code 64)。

---

## 3. 核心概念(对照本项目理解)

| 概念 | 类比 | 本项目里的实例 |
|---|---|---|
| 索引 (Index) | 数据库里的"表" | `rag_knowledge_base_v2` |
| 文档 (Document) | 一行记录 | 一个文本块(约 320 字) |
| 字段 (Field) | 一列 | `content` / `vector` / `metadata` 等 |
| 映射 (Mapping) | 表结构定义 | 见第 4 节 |
| 分片 (Shard) | 表的水平分块 | 本项目 1 个分片(数据量小,足够) |
| 副本 (Replica) | 分片的备份 | 0 个(单机,不做冗余) |
| kNN | 向量近邻检索 | HNSW 图索引,余弦相似度 |
| BM25 | 关键词打分 | 基于 jieba 分词后的 `content_seg` 字段 |
| 分析器 (Analyzer) | 分词规则 | 自定义 `cn_word`:空格分词 + 小写化 |

---

## 4. 本项目索引结构详解

索引名:`rag_knowledge_base_v2`(可在 `OPENSEARCH_INDEX` 环境变量改),
由 `backend/app/rag/store.py` 的 `create_index()` 创建。逐字段说明:

```yaml
content:      { type: text }            # 文本块原文(不指定 analyzer,存原始文本)
content_seg:  { type: text, analyzer: cn_word }   # jieba 分词结果,词与词用空格分隔
metadata:     { type: object, enabled: false }    # 元数据(文件名/页码等),只存不索引,不占检索空间
metadata_source: { type: keyword }      # 来源文件名(精确匹配用)
metadata_page:   { type: integer }     # 页码(排序/过滤用)
vector:       { type: knn_vector, dimension: 768,
                method: { name: hnsw, space_type: cosinesimil, engine: lucene } }
```

要点:

- **为什么 content 和 content_seg 分开**:OpenSearch 默认分词器对中文不友好
  (按字或按空白切),所以项目先用 jieba 把中文切成词、用空格连接存进
  `content_seg`,再配 `whitespace` 分词器——实现"词级 BM25"
- **为什么中文向量维度是 768**:BGE base 模型的输出维度(API 模式换 bge-m3 是 1024,
  建索引时自动取当前嵌入模型的维度,见 `service.py` 的 `dimension=self.embeddings.dimension`)
- **vector 用 HNSW 图**:一种近似最近邻算法,检索快(毫秒级),代价是建索引时多点内存
- **写入的 _id 是内容哈希(MD5)**:同一段文本重复写入会覆盖而不是堆积——增量建索引的基础

**索引账本**(不在 OpenSearch 里,在项目 `opensearch_meta/rag_knowledge_base_v2/`
目录下的 `manifest.json` 与 `chunks.json`——按索引名分目录):
记录每个文档文件的指纹(哈希)与 parent 副本,支撑增量索引——文件没变就跳过,
只处理新增/修改。

---

## 5. 日常运维命令(照抄可用)

> 本机原生和 Docker 容器都监听 9200,命令完全一样。
> Windows PowerShell 里 curl 已是别名,直接用。

### 5.1 健康与状态

```bash
# 集群健康:status 为 green 表示正常
curl http://localhost:9200/_cluster/health

# 列出所有索引及文档数
curl http://localhost:9200/_cat/indices?v

# 查看某索引的映射(结构)
curl http://localhost:9200/rag_knowledge_base_v2/_mapping?pretty

# 查看某索引的文档总数
curl http://localhost:9200/rag_knowledge_base_v2/_count

# 查看节点信息(版本/内存/磁盘)
curl http://localhost:9200/_nodes/stats?pretty
```

### 5.2 搜索

```bash
# 关键词搜索(BM25,走 content_seg)
curl "http://localhost:9200/rag_knowledge_base_v2/_search?pretty" -H "Content-Type: application/json" -d '{"query":{"match":{"content_seg":"示例 关键词"}}}'

# 查看一条文档原文
curl "http://localhost:9200/rag_knowledge_base_v2/_search?pretty" -H "Content-Type: application/json" -d '{"query":{"match_all":{}},"size":1}'

# 按来源文件过滤
curl "http://localhost:9200/rag_knowledge_base_v2/_search?pretty" -H "Content-Type: application/json" -d '{"query":{"term":{"metadata_source":"某文件.md"}}}'
```

### 5.3 重建索引(项目里最常用的"修复手段")

索引脏了、维度对不上、内容乱了——**直接在项目里重建**:

```bash
# 方式一:项目脚本(推荐,自动重新嵌入全部文档)
cd rag_knowledge_base/backend
python build_index_v2.py

# 方式二:前端知识库页面点「重建索引」
```

不推荐手动删索引(curl DELETE)再等应用自动建——手动删除后应用首次查询才会懒建,
期间接口报"索引不存在"。用上面的方式一最稳。

### 5.4 备份与迁移

**好消息:本项目索引不需要备份。** `data/` 目录里的原始文档 + `manifest.json`
账本 + 嵌入模型 = 随时能完整重建。真正的备份对象是:

- `data/`(你的文档)
- `backend/.env.local`(MySQL 连接等)
- MySQL 里的会话/记忆(数据库 dump:`mysqldump` 或 Navicat 导出)

如果要迁移到另一台机器,拷贝这三个即可,OpenSearch 本身不用搬。

### 5.5 看日志

```powershell
# 原生:启动窗口就是日志;或 logs/ 目录下按日期命名的文件
# Docker:
docker compose logs opensearch | tail -50
```

---

## 6. 故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| `Connection refused` / 9200 连不上 | OpenSearch 没启动 | 启动命令见第 2 节;或 `docker compose ps` 看容器状态 |
| 建索引报维度冲突(768 vs 1024) | 换过嵌入模型(本地→API bge-m3) | 项目内重建索引(第 5.3 节);Docker 环境则 `docker compose down -v` |
| 启动报 `vm.max_map_count` 过低 | Linux 内核参数 | `sudo sysctl -w vm.max_map_count=262144`(Windows 无需) |
| 启动即退出 code 64 | 安全插件配置重复 | 只留 `DISABLE_SECURITY_PLUGIN=true` 一个 |
| 内存占用高 | JVM 堆 + Lucene 缓存 | 调小 `OPENSEARCH_JAVA_OPTS`;原生改 `config/jvm.options` |
| 搜索很慢/超时 | 首次启动或索引刚重建 | 等几秒重试;查询超时在 `store.py` 有 180s |
| 磁盘快满 | 索引膨胀 | 项目内重建索引(旧索引自动覆盖);日志定期清理 |

---

## 7. 面试一句话

"知识库向量化后存进 OpenSearch:文本块原文 + jieba 分词文本 + 768 维 BGE 向量
同库存储,检索时 kNN 语义召回与 BM25 关键词召回并行,RRF 按排名融合,
再由本地 reranker 精排;增量建索引靠内容哈希做 _id 和 manifest 指纹账本,
文件没变直接跳过,只有新增/修改才重嵌入。"
