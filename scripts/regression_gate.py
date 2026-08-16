"""RAGAS 回归门槛：跑知识库评测并按阈值判定通过/不通过（CI/手动回归用）。

用法（需后端已启动，评测会真实调用模型，耗时数分钟）：
    python scripts/regression_gate.py                # 全量 kb 题集
    python scripts/regression_gate.py --limit 3      # 只跑前 3 题快检
    python scripts/regression_gate.py --min-faithfulness 0.9

流程：
1. 调 backend/eval_ragas.py 评测（结果追加到 ragas_baseline.jsonl）；
2. 取本次新增行的 faithfulness 均值与阈值比较；
3. 低于阈值退出码 1（可直接接 CI / pre-push 钩子）。

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


def count_lines(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except Exception:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="RAGAS 回归门槛")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全部）")
    parser.add_argument("--min-faithfulness", type=float, default=0.85)
    parser.add_argument("--min-contexts", type=float, default=1.0, help="平均引用来源数下限")
    args = parser.parse_args()

    before = count_lines(REPORT)
    cmd = [sys.executable, str(EVAL_SCRIPT)]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    print("执行：", " ".join(cmd), flush=True)
    ret = subprocess.call(cmd)
    if ret != 0:
        print(f"✖ 评测脚本退出码 {ret}，判不通过")
        return 1

    lines = REPORT.read_text(encoding="utf-8").splitlines()
    new_rows = []
    for line in lines[before:]:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row.get("faithfulness"), (int, float)):
            new_rows.append(row)
    if not new_rows:
        print("✖ 本次评测没有产出有效指标行，判不通过")
        return 1

    faiths = [r["faithfulness"] for r in new_rows]
    avg_faith = sum(faiths) / len(faiths)
    avg_ctx = sum(r.get("n_contexts") or 0 for r in new_rows) / len(new_rows)
    worst = min(faiths)
    print(f"题数 {len(new_rows)} · faithfulness 均值 {avg_faith:.3f}（最低 {worst:.3f}）· 平均来源 {avg_ctx:.1f}")

    failed = []
    if avg_faith < args.min_faithfulness:
        failed.append(f"faithfulness 均值 {avg_faith:.3f} < 阈值 {args.min_faithfulness}")
    if avg_ctx < args.min_contexts:
        failed.append(f"平均来源数 {avg_ctx:.1f} < 阈值 {args.min_contexts}")
    if failed:
        print("✖ 回归门槛未通过：")
        for f in failed:
            print("   -", f)
        return 1
    print("✔ 回归门槛通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
