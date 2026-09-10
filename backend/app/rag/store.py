"""OpenSearch 存储层：索引管理、批量写入、kNN / BM25 检索。

直接使用 REST API（httpx），不依赖 opensearch-py。

命名约定（v3，见 docs/INDEX_REDESIGN_PLAN.md）：
- 逻辑索引名（index_name）= 别名，读写检索都经由它；
- 物理索引 = {逻辑名}_{模型标签}_{维度}_v{schema版本}_{时间戳}；
- 全量重建写新物理索引后原子切换别名（蓝绿，可回滚），再清理旧物理索引；
- 兼容：解析不到别名但存在同名物理索引（旧脚本显式指定索引名）时按裸索引使用。

索引结构：
  - content                  原始文本
  - content_seg              jieba 分词后的文本（whitespace 分词器，供 BM25 词级匹配）
  - metadata                 元数据（只存不建索引）
  - metadata_source          文件名（keyword，展示/兼容）
  - metadata_relative_path   文档相对路径（keyword，per-doc 删除/过滤的精确键）
  - metadata_page            页码（integer）
  - vector                   knn_vector（HNSW + 余弦相似度）

块 _id 为文档内内容寻址：md5(relative_path + "\\x00" + content)。
同文件同内容重写 = 覆盖（幂等）；跨文件同内容 = 两块并存、各删各的。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time

import httpx
from langchain_core.documents import Document

from .utils import jieba_tokenize

logger = logging.getLogger(__name__)

# 索引 schema 版本：改块 ID 方案 / 元数据字段 / 命名语义时 +1（自动触发全量重建）
# 1 = 单块；2 = parent-child + metadata_source；3 = 文档内寻址 + relative_path + 别名制
IDX_SCHEMA_VERSION = 3


def slugify(text: str) -> str:
    """把模型名转成索引名安全标签（bge-base-zh-v1.5 → bge_base_zh_v1_5）。"""
    return re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_").lower() or "idx"


def chunk_doc_id(relative_path: str, content: str) -> str:
    """块 _id：文档内内容寻址（同文件同内容稳定，跨文件互不干扰）。"""
    key = f"{relative_path}\x00{content}" if relative_path else content
    return hashlib.md5(key.encode("utf-8")).hexdigest()


class OpenSearchStore:
    """OpenSearch 客户端封装（index_name 为逻辑名/别名）。"""

    def __init__(
        self,
        url: str = "http://localhost:9200",
        index_name: str = "rag_knowledge_base",
        dimension: int = 768,
        timeout: float = 180,
    ):
        self.url = url.rstrip("/")
        self.index_name = index_name
        self.dimension = dimension
        # 测试环境 plugins.security.disabled=true，无需认证
        # Accept-Encoding 不含 zstd：httpx 0.28 装了 zstandard 时会声明 zstd，
        # 但没有 zstd 解码器，OpenSearch 回 zstd 响应时读 body 挂到超时
        self.client = httpx.Client(
            base_url=self.url,
            timeout=timeout,
            headers={"Accept-Encoding": "gzip, deflate"},
        )

    # ---------- 基础操作 ----------

    def ping(self) -> dict:
        """连接检查，返回 OpenSearch 版本信息。"""
        return self._request("GET", "/")

    def _request(self, method: str, path: str, **kwargs) -> dict:
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    # ---------- 索引解析（别名 → 物理索引） ----------

    def _alias_target(self, alias: str | None = None) -> str | None:
        """别名指向的物理索引名；不存在（或不是别名）时返回 None。"""
        name = alias or self.index_name
        response = self.client.get(f"/_alias/{name}")
        if response.status_code != 200:
            return None
        try:
            data = response.json()
        except Exception:
            return None
        names = sorted(data.keys())
        return names[0] if names else None

    def resolve(self) -> tuple[str | None, bool]:
        """解析当前可写索引：返回 (物理索引名, 是否经别名解析)。

        别名解析优先；退化兼容同名物理索引（旧脚本显式指定索引名的场景）。
        """
        target = self._alias_target()
        if target:
            return target, True
        if self.client.head(f"/{self.index_name}").status_code == 200:
            return self.index_name, False
        return None, False

    def resolve_write_index(self) -> str | None:
        return self.resolve()[0]

    def index_exists(self) -> bool:
        return self.resolve()[0] is not None

    def create_index(self, name: str, dimension: int | None = None) -> None:
        """按固定名创建 k-NN 索引（裸索引场景；别名场景用 create_physical_index）。"""
        self._request("PUT", f"/{name}", json=self._index_body(dimension))
        logger.info("索引 %s 创建完成（维度 %d）", name, dimension or self.dimension)

    def create_physical_index(self, base: str, dimension: int | None = None) -> str:
        """创建带时间戳的物理索引，返回其名字（不挂别名，写完由 swap_alias 切换）。"""
        physical = f"{base}_{time.strftime('%Y%m%d_%H%M%S')}"
        self._request("PUT", f"/{physical}", json=self._index_body(dimension))
        logger.info("物理索引 %s 创建完成（维度 %d）", physical, dimension or self.dimension)
        return physical

    def swap_alias(self, physical: str, alias: str | None = None) -> None:
        """把别名原子指向新物理索引（先摘旧挂载，再挂新的）。"""
        name = alias or self.index_name
        actions = []
        current = self._alias_target(name)
        if current and current != physical:
            actions.append({"remove": {"index": current, "alias": name}})
        actions.append({"add": {"index": physical, "alias": name}})
        self._request("POST", "/_aliases", json={"actions": actions})
        logger.info("别名 %s → %s", name, physical)

    def list_physical_indices(self, prefix: str) -> list[str]:
        """列出以 prefix 开头的物理索引（用于重建后清理旧索引）。"""
        if not prefix:
            return []
        response = self.client.get(f"/_cat/indices/{prefix}*?format=json&h=index")
        if response.status_code != 200:
            return []
        try:
            rows = response.json()
        except Exception:
            return []
        return sorted(
            {
                str(row.get("index", ""))
                for row in rows
                if str(row.get("index", "")).startswith(prefix)
            }
        )

    def delete_index(self, name: str | None = None) -> None:
        target = name or self.index_name
        self._request("DELETE", f"/{target}")
        logger.info("索引 %s 已删除", target)

    def doc_count(self, index: str | None = None) -> int:
        data = self._request("GET", f"/{index or self.index_name}/_count")
        return int(data.get("count", 0))

    def _index_body(self, dimension: int | None = None) -> dict:
        return {
            "settings": {
                "index": {
                    "knn": True,
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                },
                "analysis": {
                    "analyzer": {
                        "cn_word": {
                            "type": "custom",
                            "tokenizer": "whitespace",
                            "filter": ["lowercase"],
                        }
                    }
                },
            },
            "mappings": {
                "properties": {
                    "content": {"type": "text"},
                    "content_seg": {"type": "text", "analyzer": "cn_word"},
                    "metadata": {"type": "object", "enabled": False},
                    "metadata_source": {"type": "keyword"},
                    "metadata_relative_path": {"type": "keyword"},
                    "metadata_page": {"type": "integer"},
                    "vector": {
                        "type": "knn_vector",
                        "dimension": dimension or self.dimension,
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": "lucene",
                        },
                    },
                }
            },
        }

    # ---------- 写入 ----------

    def bulk_index(
        self,
        chunks: list[Document],
        embeddings,
        batch_size: int = 64,
        target: str | None = None,
    ) -> int:
        """本地嵌入后，把原文、分词文本、元数据、向量批量写入 OpenSearch。"""
        if not chunks:
            return 0
        index = target or self.index_name
        total = 0
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            vectors = embeddings.embed_documents(
                [doc.page_content for doc in batch],
                batch_size=batch_size,
            )
            lines: list[str] = []
            for doc, vector in zip(batch, vectors):
                rel_path = str(doc.metadata.get("relative_path") or "")
                lines.append(
                    json.dumps(
                        {
                            "index": {
                                "_index": index,
                                "_id": chunk_doc_id(rel_path, doc.page_content),
                            }
                        }
                    )
                )
                lines.append(
                    json.dumps(
                        {
                            "content": doc.page_content,
                            "content_seg": " ".join(jieba_tokenize(doc.page_content)),
                            "metadata": doc.metadata,
                            "metadata_source": doc.metadata.get("source") or "",
                            "metadata_relative_path": rel_path,
                            "metadata_page": doc.metadata.get("page"),
                            "vector": vector,
                        },
                        ensure_ascii=False,
                    )
                )
            response = self.client.post(
                "/_bulk",
                content=("\n".join(lines) + "\n").encode("utf-8"),
                headers={"Content-Type": "application/x-ndjson"},
            )
            response.raise_for_status()
            result = response.json()
            if result.get("errors"):
                raise RuntimeError(
                    f"批量写入 OpenSearch 失败："
                    f"{json.dumps(result.get('items', [])[:3], ensure_ascii=False)}"
                )
            total += len(batch)
        self._request("POST", f"/{index}/_refresh")
        logger.info("已向 OpenSearch 写入 %d 个文本块", total)
        return total

    # ---------- 删除 ----------

    def delete_by_query(
        self,
        query: dict,
        target: str | None = None,
        refresh: bool = True,
    ) -> int:
        """按查询删除（per-doc 删块用），返回删除条数。"""
        index = target or self.index_name
        path = f"/{index}/_delete_by_query"
        if refresh:
            path += "?refresh=true"
        data = self._request("POST", path, json={"query": query})
        return int(data.get("deleted", 0))

    def delete_by_document(self, relative_path: str, target: str | None = None) -> int:
        """删除某个文档（relative_path）的全部向量块。"""
        deleted = self.delete_by_query(
            {"term": {"metadata_relative_path": relative_path}},
            target=target,
        )
        logger.info("已删除文档 %s 的 %d 个块", relative_path, deleted)
        return deleted

    # ---------- 检索 ----------

    def search_vector(self, query_vector: list[float], k: int = 40) -> list[Document]:
        """稠密检索：kNN 向量相似度（OpenSearch 3.x 新版 knn 查询语法）。"""
        body = {
            "query": {"knn": {"vector": {"vector": query_vector, "k": k}}},
            "size": k,
        }
        data = self._request("POST", f"/{self.index_name}/_search", json=body)
        return [self._hit_to_doc(hit) for hit in data["hits"]["hits"]]

    def search_bm25(self, query: str, k: int = 40) -> list[Document]:
        """稀疏检索：BM25 关键词匹配（jieba 分词后查 content_seg）。"""
        seg_query = " ".join(jieba_tokenize(query))
        body = {
            "query": {"match": {"content_seg": seg_query}},
            "size": k,
        }
        data = self._request("POST", f"/{self.index_name}/_search", json=body)
        return [self._hit_to_doc(hit) for hit in data["hits"]["hits"]]

    @staticmethod
    def _hit_to_doc(hit: dict) -> Document:
        source = hit.get("_source", {})
        doc = Document(
            page_content=source.get("content", ""),
            metadata=dict(source.get("metadata") or {}),
        )
        doc.metadata["_id"] = hit.get("_id")
        doc.metadata["score"] = round(hit.get("_score") or 0.0, 4)
        return doc
