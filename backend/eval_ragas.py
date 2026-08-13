"""RAGAS 基线评测：对知识库问答计算 faithfulness / answer_relevancy。

用法（backend 目录）：
    python eval_ragas.py --limit 3

说明：
- 逐题调用 /api/chat（RAG 管道），把来源内容作为 retrieved_contexts；
- 评测裁判使用 DeepSeek 官方供应商（不走免费档限流）；
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
from ragas.metrics import Faithfulness

BASE = "http://127.0.0.1:8000/api"
REPORT = Path(__file__).resolve().parent / "ragas_baseline.jsonl"


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
        "deepseek-v4-flash",
        client=client,
        max_tokens=4096,
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
    args = parser.parse_args()
    questions = json.loads(
        (Path(__file__).resolve().parent / "eval_questions.json").read_text(
            encoding="utf-8"
        )
    )
    kb = [q for q in questions if q["category"] == "kb"]
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
        samples.append(
            SingleTurnSample(
                user_input=q["question"],
                response=data.get("answer", ""),
                retrieved_contexts=contexts,
            )
        )
        rows.append(
            {
                "id": q["id"],
                "question": q["question"],
                "n_contexts": len(contexts),
                "answer_head": (data.get("answer") or "")[:120],
            }
        )

    dataset = EvaluationDataset(samples=samples)
    result = evaluate(
        dataset,
        metrics=[Faithfulness(llm=judge)],
        llm=judge,
    )
    df = result.to_pandas()
    print(df.to_string(index=False))

    for row, (_, metrics) in zip(rows, df.iterrows()):
        row.update(
            {
                "faithfulness": float(metrics.get("faithfulness")),
            }
        )
    with REPORT.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("结果已写入：", REPORT)


if __name__ == "__main__":
    main()
