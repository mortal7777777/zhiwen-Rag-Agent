"""公开基准评测：CMRC2018 段落检索 + 可选生成（EM/F1）。

目的：把本项目的检索管线（kNN+BM25+RRF+本地 BGE 精排）放到公开中文阅读理解
基准上量一次，回答"业内什么水平"（自建 21/31 题集无法对外可比）。

用法（backend 目录）：
    python eval_cmrc.py --limit 120            # 构建/复用 cmrc2018_eval 索引并评测检索
    python eval_cmrc.py --limit 120 --rebuild  # 重建索引
    python eval_cmrc.py --limit 120 --gen      # 追加生成评测（EM/F1，约 120 次 LLM 调用）

说明：
- 数据：cmrc2018_dev.json（backend 目录，gitignored；来源 github.com/ymcui/cmrc2018
  data/ 目录直链下载）；
- 语料：dev 全量文章的段落（每段 = 一个检索单元，parent=child）；
- 指标：精排后 recall@k / MRR（主口径）；融合池 recall@20（对照精排增益）；
  --gen 时用对话模型基于 top-k 段落回答并算字符级 EM/F1（中文去空白）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from langchain_core.documents import Document

from evaluate_retrieval import bootstrap_runtime

DATA = Path(__file__).resolve().parent / "cmrc2018_dev.json"
INDEX = "cmrc2018_eval"


def load_corpus_and_questions(limit: int):
    """CMRC2018 dev（GitHub 简化版：list[{context_id, context_text, qas, title}]）。"""
    data = json.loads(DATA.read_text(encoding="utf-8"))
    paragraphs: list[Document] = []
    questions: list[dict] = []
    for item in data:
        key = item.get("context_id") or item.get("title") or "untitled"
        title = item.get("title") or key
        paragraphs.append(
            Document(
                page_content=item.get("context_text") or "",
                metadata={
                    "source": f"{title}（{key}）",
                    "relative_path": f"cmrc2018/{key}",
                    "parent_id": key,
                },
            )
        )
        for qa in item.get("qas", []):
            questions.append(
                {
                    "id": qa.get("query_id"),
                    "question": qa.get("query_text", ""),
                    "gold_key": key,
                    "gold_answers": [str(a) for a in (qa.get("answers") or [""])],
                    "gold": str((qa.get("answers") or [""])[0]),
                }
            )
    # 题目切片：均匀采样保证覆盖面（跨全部 848 篇文章），可复现
    step = max(1, len(questions) // max(1, limit))
    sampled = questions[::step][:limit]
    return paragraphs, sampled


def ensure_index(store, paragraphs, embeddings, rebuild: bool) -> None:
    if rebuild and store.index_exists():
        store.delete_index()
        print(f"已删除旧索引 {INDEX}")
    if store.index_exists() and store.doc_count() >= len(paragraphs) * 0.99:
        print(f"复用已有索引 {INDEX}（{store.doc_count()} 段）")
        return
    if store.index_exists():
        store.delete_index()
    store.create_index(store.index_name, dimension=store.dimension)
    t0 = time.perf_counter()
    store.bulk_index(paragraphs, embeddings, batch_size=128)
    print(f"索引完成：{store.doc_count()} 段，{time.perf_counter() - t0:.0f}s")


def norm_cn(text) -> str:
    text = text if isinstance(text, str) else str(text)  # 数据里有数字型答案
    return re.sub(r"[\s，。！？、；：\"'“”‘’（）()《》【】·]", "", text)


def em_f1(pred: str, golds: list[str]) -> tuple[float, float, float]:
    """返回 (严格 EM, 包含式 EM, F1)。生成是解释文体，严格 EM 会系统性吃亏，
    包含式 EM（金答案作为子串出现）更贴近"回答里有没有正确内容"。"""
    pred = norm_cn(pred)
    best_em, best_contains, best_f1 = 0.0, 0.0, 0.0
    for g in golds:
        g = norm_cn(g)
        if not g:
            continue
        best_em = max(best_em, 1.0 if pred == g else 0.0)
        best_contains = max(best_contains, 1.0 if g in pred else 0.0)
        common = sum(1 for ch in set(g) if ch in pred)
        prec = common / max(1, len(set(pred)))
        rec = common / max(1, len(set(g)))
        best_f1 = max(best_f1, 2 * prec * rec / max(1e-9, prec + rec))
    return best_em, best_contains, best_f1


def main() -> int:
    parser = argparse.ArgumentParser(description="CMRC2018 公开基准评测")
    parser.add_argument("--limit", type=int, default=120, help="采样题数")
    parser.add_argument("--k", type=int, default=6, help="精排后 top-k（与线上 rerank_top_k 一致）")
    parser.add_argument("--rebuild", action="store_true", help="重建索引")
    parser.add_argument("--gen", action="store_true", help="追加生成评测（EM/F1）")
    args = parser.parse_args()

    if not DATA.exists():
        print(f"✖ 数据文件不存在：{DATA}")
        return 1
    bootstrap_runtime()
    from app.config import get_settings
    from app.rag.embeddings import LocalBGEEmbeddings
    from app.rag.retriever import hybrid_search
    from app.rag.reranker import LocalReranker
    from app.rag.store import OpenSearchStore

    settings = get_settings()
    embeddings = LocalBGEEmbeddings(settings.embedding_model_dir, use_fp16=settings.embedding_fp16)
    paragraphs, questions = load_corpus_and_questions(args.limit)
    store = OpenSearchStore(
        url=settings.opensearch_url, index_name=INDEX, dimension=embeddings.dimension
    )
    ensure_index(store, paragraphs, embeddings, args.rebuild)
    reranker = LocalReranker(settings.reranker_cache_dir)

    hits1 = hits3 = hitsk = 0
    mrr = 0.0
    pool_hits20 = 0
    rows = []
    gen = None
    if args.gen:
        from app.rag.llm import DeepSeekChat
        from app.runtime_config import chat_provider_config

        cfg = chat_provider_config(settings) or {}
        gen = DeepSeekChat(cfg)

    ems, contains, f1s = [], [], []
    t0 = time.perf_counter()
    for i, q in enumerate(questions, 1):
        fused = hybrid_search(
            store,
            embeddings,
            q["question"],
            recall_k=settings.recall_k,
            candidate_pool=settings.candidate_pool,
            demote_toc=False,
        )
        pool_keys = [d.metadata.get("parent_id") for d in fused[:20]]
        if q["gold_key"] in pool_keys:
            pool_hits20 += 1
        ranked = reranker.rerank(q["question"], fused, top_k=args.k)
        keys = [d.metadata.get("parent_id") for d in ranked]
        rank = keys.index(q["gold_key"]) + 1 if q["gold_key"] in keys else 0
        if rank == 1:
            hits1 += 1
        if rank and rank <= 3:
            hits3 += 1
        if rank:
            hitsk += 1
            mrr += 1.0 / rank
        em = contains_em = f1 = None
        if gen is not None:
            context = "\n\n".join(d.page_content for d in ranked)
            try:
                answer = gen.generate(q["question"], context, history=[])
            except Exception as exc:
                answer = f"(生成失败 {exc})"
            em, contains_em, f1 = em_f1(answer, q["gold_answers"])
            ems.append(em)
            contains.append(contains_em)
            f1s.append(f1)
        rows.append({"id": q["id"], "rank": rank, "em": em, "contains": contains_em, "f1": f1})
        if i % 25 == 0:
            print(f"  ... {i}/{len(questions)}", flush=True)

    n = len(questions)
    dt = time.perf_counter() - t0
    print(f"\n===== CMRC2018 dev 切片（{n} 题，语料 {len(paragraphs)} 段，{dt:.0f}s）=====")
    print(f"融合池 recall@20 = {pool_hits20 / n:.3f}（精排前，对照）")
    print(f"精排 recall@1 = {hits1 / n:.3f} · recall@3 = {hits3 / n:.3f} · recall@{args.k} = {hitsk / n:.3f}")
    print(f"MRR（精排后） = {mrr / n:.3f}")
    if args.gen:
        print(
            f"生成 EM(严格) = {sum(ems) / n:.3f} · EM(包含) = {sum(contains) / n:.3f}"
            f" · F1 = {sum(f1s) / n:.3f}（{n} 题，解释文体，严格 EM 偏低属正常）"
        )
    out = Path(__file__).resolve().parent / "cmrc2018_eval_result.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("逐题结果：", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
