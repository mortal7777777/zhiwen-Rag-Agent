"""RAGAS 回归门槛：跑知识库评测并按阈值判定通过/不通过（CI/手动回归用）。

用法（需后端已启动，评测会真实调用模型，耗时数分钟）：
    python scripts/regression_gate.py                    # 全量评测 + 绝对阈值判定
    python scripts/regression_gate.py --skip-eval        # 不评测，直接用报告里最后一个 run 判定
    python scripts/regression_gate.py --baseline latest  # 额外与最近一次记录基线对比
    python scripts/regression_gate.py --limit 3          # 只跑前 3 题快检
    python scripts/regression_gate.py --min-faithfulness 0.9

流程：
1. （默认）调 backend/eval_ragas.py 评测，结果追加到 ragas_baseline.jsonl；
2. 取报告里最后一个 run（或 --run 指定），按 run_id 聚合指标行；
3. 绝对阈值判定 +（--baseline 时）与基线对比：任一指标低于"基线 - 容差"即失败；
4. 打印与基线的逐题 delta（单题跌 >0.3 提示）；违规退出码 1（可接 CI 钩子）。

基线文件 backend/ragas_baselines.json 由 `eval_ragas.py --tag <name>` 写入；
--baseline latest 取 ts 最新的一条。容差默认按指标给定（faith 0.03 / ac 0.08 /
prec 0.05 / rec 0.05，覆盖单次 run 的裁判波动），--tolerance 可统一切换。

注意：评测依赖 OpenSearch/DeepSeek，不适合放 pre-commit（太慢）；
建议改动检索/提示词后手动跑，或接独立的每日 CI 任务。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL_SCRIPT = ROOT / "backend" / "eval_ragas.py"
REPORT = ROOT / "backend" / "ragas_baseline.jsonl"
BASELINES = ROOT / "backend" / "ragas_baselines.json"

METRICS = ["faithfulness", "answer_correctness", "context_precision", "context_recall"]
# 基线对比容差：单次 run 的裁判波动幅度（answer_correctness 波动最大）
DEFAULT_TOLERANCE = {
    "faithfulness": 0.03,
    "answer_correctness": 0.08,
    "context_precision": 0.05,
    "context_recall": 0.05,
}
DELTA_FLAG = 0.3  # 单题跌幅超过该值提示（可能是真回归，也可能是单题波动）


def parse_report(path: Path) -> list[dict]:
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return rows


def metric_mean(rows_: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows_ if isinstance(r.get(key), (int, float))]
    return sum(vals) / len(vals) if vals else None


def load_baseline(name: str) -> tuple[str | None, dict | None]:
    if not BASELINES.exists():
        return None, None
    try:
        data = json.loads(BASELINES.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not data:
        return None, None
    if name == "latest":
        key = max(data, key=lambda k: data[k].get("ts", ""))
        return key, data[key]
    if name in data:
        return name, data[name]
    return None, None


def main() -> int:
    parser = argparse.ArgumentParser(description="RAGAS 回归门槛")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全部）")
    parser.add_argument("--skip-eval", action="store_true", help="不重新评测，直接用报告里最后一个 run 判定")
    parser.add_argument("--run", default="", help="指定 run_id（默认取报告里最后一个 run）")
    parser.add_argument("--baseline", default="", help="基线 tag（ragas_baselines.json 的键，latest=最近一条）")
    parser.add_argument("--tolerance", type=float, default=0.0, help="基线对比容差（0=按指标默认值）")
    parser.add_argument("--min-faithfulness", type=float, default=0.85)
    parser.add_argument("--min-contexts", type=float, default=1.0, help="平均引用来源数下限")
    parser.add_argument("--min-context-recall", type=float, default=None, help="context_recall 均值下限（默认不卡）")
    parser.add_argument("--min-context-precision", type=float, default=None, help="context_precision 均值下限（默认不卡）")
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=0.5,
        help="faithfulness 有效指标覆盖率下限（judge 截断/失败会写 None，过多则本次 run 不可信）",
    )
    args = parser.parse_args()

    before = len(parse_report(REPORT))
    if not args.skip_eval:
        cmd = [sys.executable, str(EVAL_SCRIPT)]
        if args.limit:
            cmd += ["--limit", str(args.limit)]
        print("执行：", " ".join(cmd), flush=True)
        ret = subprocess.call(cmd)
        if ret != 0:
            print(f"✖ 评测脚本退出码 {ret}，判不通过")
            return 1

    all_rows = parse_report(REPORT)
    run_rows = [r for r in all_rows if r.get("run_id")]
    target_run = args.run
    if not target_run and run_rows:
        target_run = run_rows[-1]["run_id"]

    if target_run:
        scope = [r for r in run_rows if r["run_id"] == target_run]
    else:
        # 旧报告（无 run_id）：回退到"评测后新增行"语义
        scope = all_rows[before:]
        target_run = "(legacy 无 run_id)"

    # skip_faithfulness 的行（S5 门槛后拒答题）按停测处理：不进覆盖率分母与均值
    active = [r for r in scope if not r.get("skip_faithfulness")]
    parsed = len(active)
    valid_rows = [r for r in active if isinstance(r.get("faithfulness"), (int, float))]
    if not valid_rows:
        print(f"✖ run {target_run} 没有产出有效指标行，判不通过")
        return 1

    coverage = len(valid_rows) / parsed if parsed else 0.0
    faith_mean = metric_mean(valid_rows, "faithfulness")
    avg_ctx = sum(r.get("n_contexts") or 0 for r in valid_rows) / len(valid_rows)
    worst = min(r["faithfulness"] for r in valid_rows)
    means = {k: metric_mean(valid_rows, k) for k in METRICS}
    print(
        f"run {target_run} · 题数 {len(valid_rows)}/{parsed} · 指标覆盖率 {coverage:.0%} · "
        f"faithfulness 均值 {faith_mean:.3f}（最低 {worst:.3f}）· 平均来源 {avg_ctx:.1f}"
    )
    print(
        "指标均值："
        + " ".join(f"{k}={None if means[k] is None else round(means[k], 3)}" for k in METRICS)
    )

    failed = []
    if coverage < args.min_coverage:
        failed.append(
            f"faithfulness 有效覆盖率 {coverage:.0%} < 阈值 {args.min_coverage:.0%}"
            "（judge 输出截断/失败过多，本次 run 不可信）"
        )
    if faith_mean < args.min_faithfulness:
        failed.append(f"faithfulness 均值 {faith_mean:.3f} < 阈值 {args.min_faithfulness}")
    if avg_ctx < args.min_contexts:
        failed.append(f"平均来源数 {avg_ctx:.1f} < 阈值 {args.min_contexts}")
    if args.min_context_recall is not None and means["context_recall"] is not None:
        if means["context_recall"] < args.min_context_recall:
            failed.append(f"context_recall 均值 {means['context_recall']:.3f} < 阈值 {args.min_context_recall}")
    if args.min_context_precision is not None and means["context_precision"] is not None:
        if means["context_precision"] < args.min_context_precision:
            failed.append(f"context_precision 均值 {means['context_precision']:.3f} < 阈值 {args.min_context_precision}")

    if args.baseline:
        tag, base = load_baseline(args.baseline)
        if base is None:
            print(f"！未找到基线「{args.baseline}」（{BASELINES}），跳过基线对比")
        else:
            cur_by_id = {r["id"]: r for r in valid_rows}
            common = sorted(set(base.get("ids") or []) & set(cur_by_id))
            if not common:
                print(f"！本次 run 与基线「{tag}」没有公共题目，跳过基线对比")
            else:
                tol = {
                    k: (args.tolerance if args.tolerance > 0 else DEFAULT_TOLERANCE[k])
                    for k in METRICS
                }
                print(
                    f"基线对比（tag={tag}，公共题 {len(common)}，"
                    f"容差 {args.tolerance if args.tolerance > 0 else '按指标默认'}）："
                )
                for k in METRICS:
                    b = base.get(k)
                    cur = metric_mean([cur_by_id[i] for i in common], k)
                    if b is None or cur is None:
                        print(f"  {k:<20} 基线 {b} / 本次 {None if cur is None else round(cur, 3)} —— 数据不全，跳过")
                        continue
                    delta = cur - b
                    mark = ""
                    if delta < -tol[k]:
                        mark = "  <-- 低于基线-容差"
                        failed.append(
                            f"{k} 均值 {cur:.3f} < 基线 {b:.3f} - 容差 {tol[k]}（公共题 {len(common)}）"
                        )
                    print(f"  {k:<20} 基线 {b:.3f} → 本次 {cur:.3f}  （Δ {delta:+.3f}）{mark}")
                # 逐题提示：单题跌幅超阈值（可能是真回归，也可能单题波动）
                base_pq = base.get("per_question") or {}
                drops = []
                for qid in common:
                    row = cur_by_id[qid]
                    for k in METRICS:
                        bv = (base_pq.get(qid) or {}).get(k)
                        cv = row.get(k)
                        if (
                            isinstance(bv, (int, float))
                            and isinstance(cv, (int, float))
                            and cv - bv < -DELTA_FLAG
                        ):
                            drops.append((qid, k, bv, cv))
                if drops:
                    print(f"  [提示] 单题跌幅 >{DELTA_FLAG}（看是回归还是波动）：")
                    for qid, k, bv, cv in drops:
                        print(f"    {qid} {k}: {bv:.3f} → {cv:.3f}")

    if failed:
        # 用 ASCII 标记：✔/✖（U+2714/U+2716）在 GBK 控制台会 UnicodeEncodeError
        print("[FAIL] 回归门槛未通过：")
        for f in failed:
            print("   -", f)
        return 1
    print("[OK] 回归门槛通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
