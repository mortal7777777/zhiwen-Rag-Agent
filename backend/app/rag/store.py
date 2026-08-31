"""OpenSearch 存储层：索引管理、批量写入、kNN / BM25 检索。

直接使用 REST API（httpx），不依赖 opensearch-py。
索引结构：
  - content      原始文本
  - content_seg  jieba 分词后的文本（whitespace 分词器，供 BM25 词级匹配）
  - metadata     元数据（只存不建索引）
  - vector       knn_vector（HNSW + 余弦相似度）
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import httpx
from langchain_core.documents import Document

from .utils import jieba_tokenize

logger = logging.getLogger(__name__)


class OpenSearchStore:
    """OpenSearch 客户端封装。"""

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

    def index_exists(self) -> bool:
        return self.client.head(f"/{self.index_name}").status_code == 200

    def create_index(self) -> None:
        """创建 k-NN 索引：文本字段 + 768 维向量字段。"""
        body = {
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
                    "metadata_page": {"type": "integer"},
                    "vector": {
                        "type": "knn_vector",
                        "dimension": self.dimension,
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": "lucene",
                        },
                    },
                }
            },
        }
        self._request("PUT", f"/{self.index_name}", json=body)
        logger.info("索引 %s 创建完成（维度 %d）", self.index_name, self.dimension)

    def delete_index(self) -> None:
        self._request("DELETE", f"/{self.index_name}")
        logger.info("索引 %s 已删除", self.index_name)

    def doc_count(self) -> int:
        data = self._request("GET", f"/{self.index_name}/_count")
        return int(data.get("count", 0))

    # ---------- 写入 ----------

    def bulk_index(
        self,
        chunks: list[Document],
        embeddings,
        batch_size: int = 64,
    ) -> int:
        """本地嵌入后，把原文、分词文本、元数据、向量批量写入 OpenSearch。"""
        if not chunks:
            return 0
        total = 0
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            vectors = embeddings.embed_documents(
                [doc.page_content for doc in batch],
                batch_size=batch_size,
            )
            lines: list[str] = []
            for doc, vector in zip(batch, vectors):
                # 用内容哈希做 _id：内容不变时重复写入会覆盖而不是堆积
                doc_id = hashlib.md5(doc.page_content.encode("utf-8")).hexdigest()
                lines.append(
                    json.dumps({"index": {"_index": self.index_name, "_id": doc_id}})
                )
                lines.append(
                    json.dumps(
                        {
                            "content": doc.page_content,
                            "content_seg": " ".join(jieba_tokenize(doc.page_content)),
                            "metadata": doc.metadata,
                            "metadata_source": doc.metadata.get("source") or "",
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
        self._request("POST", f"/{self.index_name}/_refresh")
        logger.info("已向 OpenSearch 写入 %d 个文本块", total)
        return total

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
