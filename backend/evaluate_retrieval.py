"""检索策略评估脚本。

两种模式（在 backend 目录下运行）：

1. 调试模式（默认）：对内置示例题打印稠密/稀疏/RRF/精排各级 top 列表，
   并测试不同 recall_k 下的关键词命中率（人工排查用）；
2. 批模式 --batch：读标注文件 retrieval_annotations.json（gitignored），
   对每题跑完整检索管道（service.retrieve），计算三项确定性快指标：
   - source_recall@k  正确书目进入 top-k 的比例（书名路由 S1 的效果）
   - keyword_recall@k 关键实词句进入 top-k 的比例（内容召回 S2/S3 的效果）
   - MRR              正确书目首个命中的名次倒数（排名质量）
   无 LLM 裁判、秒级完成，作为检索迭代的内环指标；RAGAS 做验收外环。

用法：
    python evaluate_retrieval.py                        # 调试模式
    python evaluate_retrieval.py --batch                # 批模式（全管道含查询扩展）
    python evaluate_retrieval.py --batch --no-expansion # 关查询扩展（隔离索引/精排层）
    python evaluate_retrieval.py --batch --k 8
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import get_settings
from app.rag.retriever import hybrid_search
from app.rag.service import RAGService

ANNOTATIONS = Path(__file__).resolve().parent / "retrieval_annotations.json"

# 问题与用于判断"是否命中"的核心关键词（调试模式示例）
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


def normalize(text: str) -> str:
    """归一化：去空白/下划线/括号书名号，统一小写（兼容 epub+pdf 双格式文件名）。"""
    return re.sub(r"[\s_（）()《》【】]+", "", str(text or "")).lower()


def bootstrap_runtime() -> None:
    """独立进程先加载 backend/.env.local 与 DB 运行时配置（与 eval_ragas 一致：
    查询扩展走对话模型，不加载会用环境变量里的旧 key/供应商）。"""
    try:
        from run import load_local_env

        load_local_env()
        import app.db.database as db_mod
        from app.runtime_config import load_overrides

        db_mod.init_db()
        if db_mod.SessionLocal is not None:
            s = db_mod.SessionLocal()
            try:
                load_overrides(s)
            finally:
                s.close()
    except Exception as exc:
        print(f"！运行时配置加载失败（{exc}），退回环境变量默认设置")


def run_batch(args: argparse.Namespace) -> int:
    ann_path = Path(args.annotations)
    if not ann_path.exists():
        print(f"✖ 标注文件不存在：{ann_path}（批模式需要它；模板见脚本注释）")
        return 1
    items = json.loads(ann_path.read_text(encoding="utf-8"))
    bootstrap_runtime()
    service = RAGService(get_settings())
    store = service.ensure_index()
    print(f"OpenSearch 文档数：{store.doc_count()} · 标注题 {len(items)} · top_k={args.k}"
          + ("（--no-expansion：跳过查询扩展）" if args.no_expansion else ""))

    rows = []
    for item in items:
        qid, question = item["id"], item["question"]
        t0 = time.perf_counter()
        try:
            docs = service.retrieve(
                question, rewrite=(False if args.no_expansion else None)
            )[: args.k]
        except Exception as exc:
            print(f"[{qid}] 检索失败：{type(exc).__name__}: {str(exc)[:100]}")
            rows.append({"id": qid, "error": True})
            continue
        dt = time.perf_counter() - t0

        want_src = normalize(item.get("expect_source") or "")
        src_rank = 0
        if want_src:
            for i, doc in enumerate(docs, 1):
                if want_src in normalize(doc.metadata.get("source") or ""):
                    src_rank = i
                    break
        kws = item.get("expect_keywords") or []
        kw_hit = None
        if kws:
            kw_hit = any(
                kw in (doc.page_content or "") for doc in docs for kw in kws
            )
        rows.append(
            {
                "id": qid,
                "rank": src_rank,
                "has_src": bool(want_src),
                "kw": kw_hit,
                "dt": dt,
                "k": len(docs),
            }
        )
        src_txt = f"源#{src_rank}" if src_rank else ("源×" if want_src else "源-")
        kw_txt = "词√" if kw_hit else ("词×" if kw_hit is not None else "词-")
        print(f"[{qid}] {src_txt} {kw_txt} {dt:5.1f}s  {question[:30]}")

    ok = [r for r in rows if not r.get("error")]
    src_rows = [r for r in ok if r.get("has_src")]
    hits = [1 if r["rank"] else 0 for r in src_rows]
    mrr = sum((1.0 / r["rank"]) if r["rank"] else 0.0 for r in src_rows) / max(1, len(src_rows))
    kw_rows = [r for r in ok if r["kw"] is not None]
    kw_recall = (
        sum(1 for r in kw_rows if r["kw"]) / len(kw_rows) if kw_rows else None
    )
    avg_dt = sum(r["dt"] for r in ok) / max(1, len(ok))

    print("\n===== 汇总 =====")
    print(f"题数 {len(ok)}/{len(items)} · 平均检索 {avg_dt:.1f}s/题")
    print(f"source_recall@{args.k} = {sum(hits)}/{len(src_rows)} = {sum(hits)/max(1,len(src_rows)):.2f}")
    print(f"MRR(源)              = {mrr:.3f}")
    if kw_recall is not None:
        print(f"keyword_recall@{args.k} = {sum(1 for r in kw_rows if r['kw'])}/{len(kw_rows)} = {kw_recall:.2f}")
    missed_src = [r["id"] for r in src_rows if not r["rank"]]
    missed_kw = [r["id"] for r in kw_rows if not r["kw"]]
    if missed_src:
        print("未命中书目：" + ", ".join(missed_src))
    if missed_kw:
        print("关键词未命中：" + ", ".join(missed_kw))
    return 0


def run_debug() -> None:
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


def main() -> int:
    parser = argparse.ArgumentParser(description="检索策略评估")
    parser.add_argument("--batch", action="store_true", help="批模式：读标注文件算快指标")
    parser.add_argument(
        "--annotations",
        default=str(ANNOTATIONS),
        help="标注文件路径（默认 backend/retrieval_annotations.json）",
    )
    parser.add_argument("--k", type=int, default=6, help="判定用的 top-k（默认 6，与线上 rerank_top_k 一致）")
    parser.add_argument("--no-expansion", action="store_true", help="批模式关查询扩展（隔离索引/精排层）")
    args = parser.parse_args()

    if args.batch:
        return run_batch(args)
    run_debug()
    return 0


if __name__ == "__main__":
    sys.exit(main())
