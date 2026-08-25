"""RAGAS 基线评测：检索 + 生成双维度指标。

用法（backend 目录）：
    python eval_ragas.py --limit 3

指标（按可用数据自动组合）：
- Faithfulness        生成是否忠于检索上下文（无需参考答案，必测）；
- AnswerCorrectness   回答与参考答案的事实一致性（题目带 reference 时测）；
- ContextPrecision    检索上下文与参考答案相关度（带 reference 时测）；
- ContextRecall       参考答案被检索上下文覆盖的比例（带 reference 时测）。

说明：
- 逐题调用 /api/chat（RAG 管道），把来源内容作为 retrieved_contexts；
- 题库优先读本地 eval_questions.local.json（含知识库书目与参考答案，
  gitignored）；不存在才用公开版；
- 结果写入 ragas_baseline.jsonl，可在改动前后对比回归。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from openai import OpenAI
from ragas import EvaluationDataset, SingleTurnSample, evaluate
from ragas.llms import llm_factory
from ragas.metrics import (
    AnswerCorrectness,
    ContextPrecision,
    ContextRecall,
    Faithfulness,
)

BASE = "http://127.0.0.1:8000/api"
REPORT = Path(__file__).resolve().parent / "ragas_baseline.jsonl"


def load_questions() -> list[dict]:
    """优先本地题库（含参考答案），否则公开版。"""
    local = Path(__file__).resolve().parent / "eval_questions.local.json"
    path = local if local.exists() else Path(__file__).resolve().parent / "eval_questions.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return [q for q in data if q["category"] == "kb"]


def load_judge():
    from app.config import get_settings
    from app.runtime_config import get_providers

    settings = get_settings()
    providers = get_providers(settings)
    cfg = next((p for p in providers if p.get("id") == "deepseek"), None)
    api_key = (cfg or {}).get("api_key") or os.environ.get("DEEPSEEK_API_KEY") or ""
    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )
    return llm_factory(
        "deepseek-chat",
        client=client,
        max_tokens=8192,
        temperature=0,
    )


def rag_chat(question: str) -> dict:
    body = json.dumps(
        {"question": question, "history": []}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--id", default="")
    args = parser.parse_args()
    kb = load_questions()
    if args.id:
        kb = [q for q in kb if q["id"] == args.id]
    if args.limit:
        kb = kb[: args.limit]

    judge = load_judge()
    samples = []
    rows = []
    for q in kb:
        data = None
        for attempt in range(2):
            try:
                data = rag_chat(q["question"])
                break
            except Exception as exc:
                print(f"[{q['id']}] 重试 {attempt + 1}：{str(exc)[:80]}", flush=True)
                time.sleep(6)
        if data is None:
            continue
        contexts = [s.get("content", "") for s in (data.get("sources") or [])]
        sample_kwargs = dict(
            user_input=q["question"],
            response=data.get("answer", ""),
            retrieved_contexts=contexts,
        )
        reference = (q.get("reference") or "").strip()
        if reference:
            sample_kwargs["reference"] = reference
        samples.append(SingleTurnSample(**sample_kwargs))
        rows.append(
            {
                "id": q["id"],
                "question": q["question"],
                "n_contexts": len(contexts),
                "has_reference": bool(reference),
                "answer_head": (data.get("answer") or "")[:120],
            }
        )

    # 指标按可用数据组合：faithfulness 必测；带参考答案的题追加三个指标
    metrics = [Faithfulness(llm=judge)]
    if any(r["has_reference"] for r in rows):
        metrics += [
            AnswerCorrectness(llm=judge),
            ContextPrecision(llm=judge),
            ContextRecall(llm=judge),
        ]
    # ragas to_pandas 的列名是 snake_case（faithfulness / answer_correctness ...）
    COLUMN_KEYS = {
        "Faithfulness": "faithfulness",
        "AnswerCorrectness": "answer_correctness",
        "ContextPrecision": "context_precision",
        "ContextRecall": "context_recall",
    }
    metric_names = [type(m).__name__ for m in metrics]
    print(f"指标：{', '.join(metric_names)}（参考答案仅 {sum(r['has_reference'] for r in rows)}/{len(rows)} 题有，无参考的题该三项为 None）\n")

    dataset = EvaluationDataset(samples=samples)
    result = evaluate(
        dataset,
        metrics=metrics,
        llm=judge,
    )
    df = result.to_pandas()
    print(df.to_string(index=False))

    for row, (_, mrow) in zip(rows, df.iterrows()):
        for name in metric_names:
            key = COLUMN_KEYS[name]
            value = mrow.get(key)
            row[key] = None if value is None else float(value)
    with REPORT.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    # 汇总
    with_reference = [r for r in rows if r["has_reference"]]
    if with_reference:
        def _avg(rows_, key):
            vals = [r[key] for r in rows_ if r.get(key) is not None]
            return round(sum(vals) / len(vals), 3) if vals else None

        print(
            "\n带参考答案汇总（{} 题）: faithfulness={} answer_correctness={} "
            "context_precision={} context_recall={}".format(
                len(with_reference),
                _avg(rows, "faithfulness"),
                _avg(with_reference, "answer_correctness"),
                _avg(with_reference, "context_precision"),
                _avg(with_reference, "context_recall"),
            )
        )
    print("结果已写入：", REPORT)


if __name__ == "__main__":
    main()
