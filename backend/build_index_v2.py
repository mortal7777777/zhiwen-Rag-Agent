"""构建 v2 索引（Parent-Child 切分 + 新检索字段）。

用法：
    $env:OPENSEARCH_INDEX="rag_knowledge_base_v2"
    python build_index_v2.py
"""

from __future__ import annotations

import os

os.environ.setdefault("OPENSEARCH_INDEX", "rag_knowledge_base_v2")

from app.config import get_settings  # noqa: E402
from app.rag.service import RAGService  # noqa: E402


def main() -> None:
    service = RAGService(get_settings())
    store = service.ensure_index(force_rebuild=True)
    print(f"v2 索引构建完成：OpenSearch 文档数 {store.doc_count()}")


if __name__ == "__main__":
    main()
