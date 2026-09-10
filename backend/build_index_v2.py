"""构建索引（Parent-Child 切分 + 文档内寻址，蓝绿重建到别名）。

用法：
    $env:OPENSEARCH_INDEX="zhiwen_kb_current"
    python build_index_v2.py
"""

from __future__ import annotations

import os

os.environ.setdefault("OPENSEARCH_INDEX", "zhiwen_kb_current")

from app.config import get_settings  # noqa: E402
from app.rag.service import RAGService  # noqa: E402


def main() -> None:
    service = RAGService(get_settings())
    store = service.ensure_index(force_rebuild=True)
    print(f"索引构建完成：OpenSearch 文档数 {store.doc_count()}")


if __name__ == "__main__":
    main()
