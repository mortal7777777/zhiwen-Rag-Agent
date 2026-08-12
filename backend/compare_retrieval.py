"""新旧检索方案对比。

用法（在 backend 目录下）：
    python compare_retrieval.py rag_knowledge_base      # 旧方案（v1 索引）
    python compare_retrieval.py rag_knowledge_base_v2    # 新方案（v2 索引）

新索引会输出两种模式：
    Parent-Child            只启用小到大检索
    Parent-Child + Query改写 启用小到大检索 + DeepSeek 查询改写
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluate_retrieval import QUESTIONS, preview  # noqa: E402


def main() -> None:
    index_name = sys.argv[1] if len(sys.argv) > 1 else "rag_knowledge_base_v2"
    # 必须在导入 app 模块前设置，get_settings 会缓存
    os.environ["OPENSEARCH_INDEX"] = index_name

    from app.config import get_settings
    from app.rag.service import RAGService

    service = RAGService(get_settings())
    store = service.ensure_index()
    print(f"索引：{index_name} | OpenSearch 文档数：{store.doc_count()}")

    if index_name == "rag_knowledge_base":
        modes = [("旧方案 child(500/80)", False, False)]
    else:
        modes = [
            ("Parent-Child", False, True),
            ("Parent-Child + Query改写", True, True),
        ]

    for label, rewrite, parent_child in modes:
        print(f"\n### {label}")
        print(f"{'关键词':<14} {'命中/4':>6} {'上下文字数':>10}  top1 预览")
        total_hits = 0
        total_chars = 0
        for question, keyword in QUESTIONS:
            docs = service.retrieve(
                question,
                rewrite=rewrite,
                parent_child=parent_child,
            )
            context = "\n\n".join(doc.page_content for doc in docs)
            hits = sum(1 for doc in docs if keyword in doc.page_content)
            total_hits += hits
            total_chars += len(context)
            top1 = preview(docs[0].page_content, 24) if docs else "-"
            print(f"{keyword:<14} {hits:>6} {len(context):>10}  {top1}")
        count = len(QUESTIONS)
        print(
            f"平均：每问命中 {total_hits / count:.2f}/4，"
            f"上下文 {total_chars / count:.0f} 字"
        )


if __name__ == "__main__":
    main()
