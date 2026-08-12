"""紧凑版检索评估：只输出各环节关键词命中率，便于快速判断策略是否合适。

运行（在 backend 目录下）：
    python retrieval_summary.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import get_settings
from app.rag.retriever import hybrid_search
from app.rag.service import RAGService
from evaluate_retrieval import QUESTIONS, hit_count, preview


def main() -> None:
    service = RAGService(get_settings())
    store = service.ensure_index()

    print(
        f"{'关键词':<14} {'DENSE/10':>9} {'SPARSE/10':>10} "
        f"{'RRF/10':>7} {'RERANK/4':>9} {'重合':>4}  RRF-top1 | RERANK-top1"
    )
    for question, keyword in QUESTIONS:
        query_vector = service.embeddings.embed_query(question)
        dense = store.search_vector(query_vector, 10)
        sparse = store.search_bm25(question, 10)
        fused = hybrid_search(
            store,
            service.embeddings,
            question,
            recall_k=40,
            candidate_pool=10,
        )
        reranked = service.reranker.rerank(question, fused, top_k=4)

        overlap = len(
            {doc.metadata.get("_id") for doc in dense}
            & {doc.metadata.get("_id") for doc in sparse}
        )
        rrf_top1 = preview(fused[0].page_content, 22) if fused else "-"
        rerank_top1 = preview(reranked[0].page_content, 22) if reranked else "-"
        print(
            f"{keyword:<14} {hit_count(dense, keyword):>9} "
            f"{hit_count(sparse, keyword):>10} {hit_count(fused, keyword):>7} "
            f"{hit_count(reranked, keyword):>9} {overlap:>4}  "
            f"{rrf_top1} | {rerank_top1}"
        )


if __name__ == "__main__":
    main()
