"""配对显著性对比：两臂逐题 Δ + bootstrap 95% CI。

背景：单题指标 run-to-run 波动大（RAGAS 单题 ±0.3~0.4），只看两臂均值差
分不清"提升是真的还是噪声"。本脚本按题号配对（同一题在两臂的结果相减），
再对配对差值做 bootstrap 置信区间：CI 不含 0 才算显著。口径参考
Rathi et al. 2026（arXiv 2609.38473）的 paired per-query 分析。

用法（仓库根或任意目录）：
    # 快指标（确定性，无 LLM；先跑两臂再对比）
    python backend/evaluate_retrieval.py --batch --no-expansion --out armA.json
    python backend/evaluate_retrieval.py --batch --no-expansion --out armB.json
    python scripts/paired_compare.py fast --a armA.json --b armB.json

    # RAGAS：读 backend/ragas_baselines.json 的两个 tag
    python scripts/paired_compare.py ragas --tag-a 2026-09-11-s3 --tag-b <新tag>
    # RAGAS：读 backend/ragas_baseline.jsonl 的两个 run_id
    python scripts/paired_compare.py ragas --run-a <run_id> --run-b <run_id>

约定：A = 基线臂，B = 实验臂；Δ = B - A（正数表示实验臂更好）。
只统计两臂都有值的题（RAGAS 指标写 None 的题自动跳过）。
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
RAGAS_BASELINES = BACKEND / "ragas_baselines.json"
RAGAS_REPORT = BACKEND / "ragas_baseline.jsonl"

FAST_METRICS = ["source_recall", "mrr", "keyword_recall", "multi_all"]
RAGAS_METRICS = ["faithfulness", "answer_correctness", "context_precision", "context_recall"]
EPS = 1e-9


def bootstrap_ci(
    deltas: list[float], iters: int = 10000, seed: int = 42, alpha: float = 0.05
) -> tuple[float, float]:
    """配对差值的 bootstrap 百分位 CI（默认 95%）。"""
    n = len(deltas)
    if n == 0:
        return (0.0, 0.0)
    rng = random.Random(seed)
    means = []
    for _ in range(iters):
        total = 0.0
        for _ in range(n):
            total += deltas[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    lo = means[max(0, int(alpha / 2 * iters))]
    hi = means[min(iters - 1, int((1 - alpha / 2) * iters))]
    return (lo, hi)


def fast_row_metrics(row: dict) -> dict[str, float | None]:
    """快轨行 → 指标值（该题不适用/缺失的指标返回 None，配对时跳过）。"""
    out: dict[str, float | None] = {}
    if row.get("has_src"):
        rank = row.get("rank") or 0
        out["source_recall"] = 1.0 if rank else 0.0
        out["mrr"] = (1.0 / rank) if rank else 0.0
    else:
        out["source_recall"] = None
        out["mrr"] = None
    kw = row.get("kw")
    out["keyword_recall"] = None if kw is None else (1.0 if kw else 0.0)
    all_src = row.get("all_src")
    out["multi_all"] = None if all_src is None else (1.0 if all_src else 0.0)
    return out


def load_fast(path: str | Path) -> dict[str, dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("rows") if isinstance(data, dict) else data
    out: dict[str, dict] = {}
    for r in rows or []:
        if r.get("error"):
            continue
        out[r["id"]] = fast_row_metrics(r)
    return out


def load_ragas_tags(tag_a: str, tag_b: str) -> tuple[dict, dict]:
    data = json.loads(RAGAS_BASELINES.read_text(encoding="utf-8"))
    missing = [t for t in (tag_a, tag_b) if t not in data]
    if missing:
        raise SystemExit(f"ragas_baselines.json 里没有 tag：{missing}；现有：{sorted(data)}")

    def arm(tag: str) -> dict:
        return dict(data[tag].get("per_question") or {})

    return arm(tag_a), arm(tag_b)


def load_ragas_runs(run_a: str, run_b: str) -> tuple[dict, dict]:
    rows = []
    for line in RAGAS_REPORT.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue

    def arm(run_id: str) -> dict:
        out: dict[str, dict] = {}
        for r in rows:
            if r.get("run_id") == run_id:
                out[r["id"]] = {k: r.get(k) for k in RAGAS_METRICS}  # 后出现的覆盖先出现的
        return out

    return arm(run_a), arm(run_b)


def compare(
    arm_a: dict, arm_b: dict, metrics: list[str], iters: int = 10000, seed: int = 42
) -> dict:
    """按题号配对，逐指标算均值/Δ/CI/胜平负。纯函数（便于单测）。"""
    common = sorted(set(arm_a) & set(arm_b))
    only_a = sorted(set(arm_a) - set(arm_b))
    only_b = sorted(set(arm_b) - set(arm_a))
    results = []
    for metric in metrics:
        pairs = []
        for qid in common:
            va = arm_a[qid].get(metric)
            vb = arm_b[qid].get(metric)
            if not isinstance(va, (int, float)) or not isinstance(vb, (int, float)):
                continue
            pairs.append((qid, float(va), float(vb)))
        n = len(pairs)
        if n == 0:
            results.append({"metric": metric, "n": 0})
            continue
        mean_a = sum(p[1] for p in pairs) / n
        mean_b = sum(p[2] for p in pairs) / n
        deltas = [p[2] - p[1] for p in pairs]
        delta = sum(deltas) / n
        lo, hi = bootstrap_ci(deltas, iters=iters, seed=seed)
        win = sum(1 for d in deltas if d > EPS)
        loss = sum(1 for d in deltas if d < -EPS)
        results.append(
            {
                "metric": metric,
                "n": n,
                "mean_a": mean_a,
                "mean_b": mean_b,
                "delta": delta,
                "ci_low": lo,
                "ci_high": hi,
                "significant": (lo > 0) or (hi < 0),
                "win": win,
                "tie": n - win - loss,
                "loss": loss,
            }
        )
    return {"results": results, "paired": len(common), "only_a": only_a, "only_b": only_b}


def print_report(rep: dict, label_a: str, label_b: str) -> None:
    print(f"A（基线）= {label_a}")
    print(
        f"B（实验）= {label_b}    配对题 {rep['paired']}"
        f"（仅 A 独有 {len(rep['only_a'])}、仅 B 独有 {len(rep['only_b'])}）"
    )
    print("Δ = B - A，正数表示实验臂更好；CI 不含 0 即显著（标 *）\n")
    head = f"{'metric':<20}{'n':>4}{'A_mean':>9}{'B_mean':>9}{'delta':>9}{'CI95':>20}{'W/T/L':>11}"
    print(head)
    print("-" * len(head))
    for r in rep["results"]:
        if not r.get("n"):
            print(f"{r['metric']:<20}{0:>4}  （两臂无共同有效题）")
            continue
        ci = f"[{r['ci_low']:+.3f},{r['ci_high']:+.3f}]"
        wtl = f"{r['win']}/{r['tie']}/{r['loss']}"
        mark = "  *" if r["significant"] else ""
        print(
            f"{r['metric']:<20}{r['n']:>4}{r['mean_a']:>9.3f}{r['mean_b']:>9.3f}"
            f"{r['delta']:>+9.3f}{ci:>20}{wtl:>11}{mark}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="配对显著性对比（逐题 Δ + bootstrap CI）")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_fast = sub.add_parser("fast", help="快指标（evaluate_retrieval.py --out 产出的 JSON）")
    p_fast.add_argument("--a", required=True, help="基线臂结果 JSON")
    p_fast.add_argument("--b", required=True, help="实验臂结果 JSON")

    p_ragas = sub.add_parser("ragas", help="RAGAS 四指标（两个 tag 或两个 run_id）")
    p_ragas.add_argument("--tag-a", default="")
    p_ragas.add_argument("--tag-b", default="")
    p_ragas.add_argument("--run-a", default="")
    p_ragas.add_argument("--run-b", default="")

    for p in (p_fast, p_ragas):
        p.add_argument("--iters", type=int, default=10000, help="bootstrap 重采样次数")
        p.add_argument("--seed", type=int, default=42)
        p.add_argument("--json", default="", help="把结果另存为 JSON")

    args = parser.parse_args()
    if args.mode == "fast":
        arm_a, arm_b = load_fast(args.a), load_fast(args.b)
        metrics = FAST_METRICS
        label_a, label_b = args.a, args.b
    else:
        if args.tag_a and args.tag_b:
            arm_a, arm_b = load_ragas_tags(args.tag_a, args.tag_b)
            label_a, label_b = f"tag:{args.tag_a}", f"tag:{args.tag_b}"
        elif args.run_a and args.run_b:
            arm_a, arm_b = load_ragas_runs(args.run_a, args.run_b)
            label_a, label_b = f"run:{args.run_a}", f"run:{args.run_b}"
        else:
            raise SystemExit("ragas 模式需要 --tag-a/--tag-b 或 --run-a/--run-b")
        metrics = RAGAS_METRICS

    rep = compare(arm_a, arm_b, metrics, iters=args.iters, seed=args.seed)
    print_report(rep, label_a, label_b)
    if args.json:
        Path(args.json).write_text(
            json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print("\n结果已写入：", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
