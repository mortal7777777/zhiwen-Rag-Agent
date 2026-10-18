"""配对统计脚本的自测（scripts/paired_compare.py）。

工具类脚本不做单测容易"静默失效"（配对逻辑改坏后仍然给出一张好看的
表格）。这里用合成数据断言：指标提取、按题配对、胜平负、CI 的基本性质。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "paired_compare.py"


def _load():
    spec = importlib.util.spec_from_file_location("paired_compare", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fast_row_metrics_rank_and_keyword():
    pc = _load()
    m = pc.fast_row_metrics({"has_src": True, "rank": 3, "kw": False})
    assert m["source_recall"] == 1.0
    assert abs(m["mrr"] - 1 / 3) < 1e-9
    assert m["keyword_recall"] == 0.0

    miss = pc.fast_row_metrics({"has_src": True, "rank": 0, "kw": None})
    assert miss["source_recall"] == 0.0
    assert miss["mrr"] == 0.0
    assert miss["keyword_recall"] is None

    no_src = pc.fast_row_metrics({"has_src": False, "rank": 0, "kw": True})
    assert no_src["source_recall"] is None
    assert no_src["mrr"] is None


def test_load_fast_skips_error_rows(tmp_path):
    pc = _load()
    path = tmp_path / "arm.json"
    path.write_text(
        json.dumps(
            {
                "rows": [
                    {"id": "q1", "rank": 2, "has_src": True, "kw": True},
                    {"id": "q2", "error": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    arm = pc.load_fast(path)
    assert set(arm) == {"q1"}
    assert abs(arm["q1"]["mrr"] - 0.5) < 1e-9


def test_compare_pairs_and_win_tie_loss():
    pc = _load()
    arm_a = {
        "q1": {"mrr": 0.5},
        "q2": {"mrr": 1.0},
        "q3": {"mrr": 0.0},
        "q4": {"mrr": None},
    }
    arm_b = {
        "q1": {"mrr": 1.0},  # win
        "q2": {"mrr": 1.0},  # tie
        "q3": {"mrr": 0.0},  # tie
        "q4": {"mrr": None},
        "q5": {"mrr": 1.0},  # 仅 B 独有，不配对
    }
    rep = pc.compare(arm_a, arm_b, ["mrr"], iters=2000, seed=1)
    r = rep["results"][0]
    assert r["n"] == 3
    assert r["win"] == 1 and r["loss"] == 0 and r["tie"] == 2
    assert abs(r["delta"] - 1 / 6) < 1e-9
    assert rep["only_b"] == ["q5"]
    assert rep["only_a"] == []


def test_fast_row_metrics_multi_source():
    pc = _load()
    miss = pc.fast_row_metrics(
        {"has_src": True, "rank": 1, "kw": True, "multi": True, "all_src": False}
    )
    assert miss["multi_all"] == 0.0
    hit = pc.fast_row_metrics(
        {"has_src": True, "rank": 1, "kw": True, "multi": True, "all_src": True}
    )
    assert hit["multi_all"] == 1.0
    single = pc.fast_row_metrics(
        {"has_src": True, "rank": 1, "kw": True, "multi": False, "all_src": None}
    )
    assert single["multi_all"] is None


def test_compare_metric_with_no_pairs_reports_zero():
    pc = _load()
    rep = pc.compare({"q1": {"mrr": None}}, {"q1": {"mrr": 1.0}}, ["mrr"], iters=100)
    assert rep["results"][0]["n"] == 0


def test_bootstrap_ci_sign_and_zero():
    pc = _load()
    lo, hi = pc.bootstrap_ci([0.1] * 40, iters=2000, seed=7)
    assert lo > 0 and hi > 0  # 全正差值 → CI 不含 0
    lo0, hi0 = pc.bootstrap_ci([0.0] * 40, iters=2000, seed=7)
    assert lo0 == 0.0 and hi0 == 0.0
    assert pc.bootstrap_ci([]) == (0.0, 0.0)


def test_compare_marks_significance():
    pc = _load()
    arm_a = {f"q{i}": {"faithfulness": 0.5} for i in range(30)}
    arm_b = {f"q{i}": {"faithfulness": 0.9} for i in range(30)}
    rep = pc.compare(arm_a, arm_b, ["faithfulness"], iters=2000, seed=3)
    r = rep["results"][0]
    assert r["significant"] is True
    assert r["ci_low"] > 0
