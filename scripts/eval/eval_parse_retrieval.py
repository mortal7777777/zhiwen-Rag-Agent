"""解析质量检索评测：国考课件 / 扫描书问题 → 命中来源与页码（hit@k）。

用法（需后端已启动）：
  python scripts/eval/eval_parse_retrieval.py
  python scripts/eval/eval_parse_retrieval.py --top-k 5 --questions scripts/eval/parse_retrieval_questions.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUESTIONS = ROOT / "scripts" / "eval" / "parse_retrieval_questions.json"


def load_questions(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def page_matches(result: dict, expected: dict) -> bool:
    page = expected.get("page")
    if page is None:
        return True
    got = result.get("page")
    if got == page:
        return True
    pages = result.get("pages") or []
    return page in pages


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    args = ap.parse_args()

    questions = load_questions(args.questions)
    hits = 0
    with httpx.Client(trust_env=False, timeout=300) as client:
        for q in questions:
            resp = client.post(
                f"{args.base_url}/api/index/search",
                params={"question": q["question"]},
            )
            resp.raise_for_status()
            results = (resp.json().get("results") or [])[: args.top_k]
            hit = False
            for r in results:
                path = str(r.get("relative_path") or "")
                if q.get("path") and q["path"] not in path:
                    continue
                if page_matches(r, q):
                    hit = True
                    break
            hits += int(hit)
            print(
                f"{'HIT ' if hit else 'MISS'} {q['id']:22s} "
                f"{q['question'][:34]:36s} "
                f"top={[ (r.get('relative_path','')[-28:], r.get('page')) for r in results[:3] ]}"
            )
    total = len(questions)
    print(f"\nhit@{args.top_k} = {hits}/{total} = {hits / total:.3f}" if total else "no questions")


if __name__ == "__main__":
    main()
