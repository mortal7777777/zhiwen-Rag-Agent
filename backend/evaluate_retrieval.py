"""检索策略评估脚本。

对一组书目类问题，分别检查：
  1. 稠密检索（OpenSearch kNN）top10
  2. 稀疏检索（OpenSearch BM25）top10
  3. RRF 融合 top10
  4. 本地 reranker 精排 top4

并测试不同 recall_k 参数下关键词命中率，判断当前参数是否合适。
运行（在 backend 目录下）：
    python evaluate_retrieval.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import get_settings
from app.rag.retriever import hybrid_search
from app.rag.service import RAGService

# 问题与用于判断"是否命中"的核心关键词
QUESTIONS = [
    ("《示例书甲》的主要观点是什么？", "示例书甲"),
    ("《示例书乙》如何分析战争的阶段划分？", "示例书乙"),
    ("“实事求是”的含义是什么？", "实事求是"),
    ("群众路线的基本内容是什么？", "群众路线"),
    ("《示例书甲》中主次矛盾的关系是什么？", "示例书甲"),
    ("新民主主义革命的总路线是什么？", "新民主主义"),
    ("《湖南农民运动考察报告》的核心结论是什么？", "湖南农民运动"),
    ("什么是“枪杆子里面出政权”？", "枪杆子里面出政权"),
]


def preview(text: str, length: int = 36) -> str:
    """截断文本便于打印。"""
    return text.replace("\n", " ").strip()[:length]


def hit_count(docs: list, keyword: str) -> int:
    """统计检索结果里包含关键词的条数。"""
    return sum(1 for doc in docs if keyword in doc.page_content)


def print_docs(label: str, docs: list, keyword: str) -> None:
    print(f"--- {label}（命中关键词 {hit_count(docs, keyword)}/{len(docs)}）---")
    for i, doc in enumerate(docs, 1):
        score = (
            doc.metadata.get("rerank_score")
            or doc.metadata.get("hybrid_score")
            or doc.metadata.get("score")
        )
        source = doc.metadata.get("source") or doc.metadata.get("_id", "")
        print(f"  [{i}] {score} | {preview(doc.page_content)} | {source}")


def test_recall_k(service: RAGService, store, question: str, keyword: str) -> None:
    """测试不同召回数量对关键词命中率的影响。"""
    print(f"\n[参数测试] {question}")
    for recall_k in (20, 40, 60, 80):
        docs = hybrid_search(
            store,
            service.embeddings,
            question,
            recall_k=recall_k,
            candidate_pool=24,
        )
        print(f"  recall_k={recall_k:>3} -> 融合后 24 条中命中关键词 {hit_count(docs, keyword)} 条")


def main() -> None:
    service = RAGService(get_settings())
    store = service.ensure_index()
    print(f"OpenSearch 文档数：{store.doc_count()}")
    print(f"当前参数：recall_k={service.settings.recall_k}, "
          f"candidate_pool={service.settings.candidate_pool}, "
          f"rerank_top_k={service.settings.rerank_top_k}")

    for question, keyword in QUESTIONS:
        print("\n" + "=" * 72)
        print("问题：", question)

        query_vector = service.embeddings.embed_query(question)
        dense = store.search_vector(query_vector, 10)
        sparse = store.search_bm25(question, 10)
        fused = hybrid_search(
            store,
            service.embeddings,
            question,
            recall_k=service.settings.recall_k,
            candidate_pool=10,
        )
        reranked = service.reranker.rerank(
            question,
            fused,
            top_k=service.settings.rerank_top_k,
        )

        dense_ids = {doc.metadata.get("_id") for doc in dense}
        sparse_ids = {doc.metadata.get("_id") for doc in sparse}
        print(f"稠密 ∩ 稀疏（top10 重合条数）：{len(dense_ids & sparse_ids)}")

        print_docs("DENSE  top10", dense, keyword)
        print_docs("SPARSE top10", sparse, keyword)
        print_docs("RRF    top10", fused, keyword)
        print_docs("RERANK top4", reranked, keyword)

    # 参数测试：只取前 3 个问题，避免输出太长
    print("\n" + "=" * 72)
    print("recall_k 参数影响测试")
    for question, keyword in QUESTIONS[:3]:
        test_recall_k(service, store, question, keyword)


if __name__ == "__main__":
    main()
