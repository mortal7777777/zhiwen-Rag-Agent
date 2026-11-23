"""派发 ROI 评测（Phase B 6.5）。

同一组题分别以 `agent_subagents_enabled` 开/关运行，比较延迟、token、子代理
调用数与答案质量，回答"什么时候 dispatch 比同轮工具更划算"。

用法（需后端已启动；脚本会临时切换设置并在 finally 恢复）：
  python scripts/eval/eval_dispatch.py --compare --limit 3
  python scripts/eval/eval_dispatch.py --compare --limit 6 --judge
  python scripts/eval/eval_dispatch.py --limit 2          # 只跑当前配置

输出：backend/eval_dispatch_report.md / .jsonl / .csv
注意：默认不清理评测会话（留痕）；`--cleanup` 会删除本脚本创建的会话。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
DEFAULT_QUESTIONS = Path(__file__).resolve().parent / "dispatch_eval_questions.json"
DEFAULT_OUT = BACKEND / "eval_dispatch_report.md"


def load_questions(path: Path = DEFAULT_QUESTIONS) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = data.get("questions") if isinstance(data, dict) else data
    return [q for q in (questions or []) if isinstance(q, dict) and q.get("id")]


def extract_run_metrics(run: dict) -> dict:
    """从 /api/runs 单条记录提取 ROI 指标（纯函数，便于单测）。"""
    usage = run.get("token_usage") or {}
    dispatch = usage.get("dispatch") or {}
    timings = usage.get("timings") or {}
    trace = run.get("tool_trace") or []
    subagent_runs = sum(
        1 for item in trace if isinstance(item, dict) and item.get("name") == "subagent"
    )
    return {
        "status": run.get("status") or "",
        "latency_ms": int(run.get("latency_ms") or 0),
        "answer_len": int(run.get("answer_len") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "aux_total_tokens": int(usage.get("aux_total_tokens") or 0),
        "subagent_llm_calls": int(usage.get("subagent_llm_calls") or 0),
        "dispatch_rounds": int(dispatch.get("rounds") or 0),
        "dispatch_subtasks": int(dispatch.get("subtasks") or 0),
        "dispatch_errors": int(dispatch.get("errors") or 0),
        "subagent_ms": int(timings.get("subagent_ms") or 0),
        "tool_calls": max(0, len(trace) - subagent_runs),
        "subagent_runs": subagent_runs,
    }


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 1) if values else 0.0


def summarize(rows: list[dict]) -> dict[str, dict]:
    """按配置聚合。rows 里每项需含 config + extract_run_metrics 的键。"""
    out: dict[str, dict] = {}
    for row in rows:
        config = str(row.get("config") or "current")
        item = out.setdefault(
            config,
            {
                "count": 0,
                "avg_latency_ms": 0.0,
                "avg_total_tokens": 0.0,
                "avg_aux_tokens": 0.0,
                "avg_subagent_calls": 0.0,
                "dispatched_questions": 0,
                "avg_dispatch_rounds": 0.0,
                "errors": 0,
                "avg_score": None,
            },
        )
        item["count"] += 1
        item.setdefault("_latency", []).append(float(row.get("latency_ms") or 0))
        item.setdefault("_tokens", []).append(float(row.get("total_tokens") or 0))
        item.setdefault("_aux", []).append(float(row.get("aux_total_tokens") or 0))
        item.setdefault("_subcalls", []).append(float(row.get("subagent_llm_calls") or 0))
        item.setdefault("_rounds", []).append(float(row.get("dispatch_rounds") or 0))
        if int(row.get("dispatch_subtasks") or 0) > 0:
            item["dispatched_questions"] += 1
        if str(row.get("status") or "") != "ok":
            item["errors"] += 1
        if row.get("score") is not None:
            item.setdefault("_scores", []).append(float(row["score"]))
    for item in out.values():
        item["avg_latency_ms"] = _avg(item.pop("_latency", []))
        item["avg_total_tokens"] = _avg(item.pop("_tokens", []))
        item["avg_aux_tokens"] = _avg(item.pop("_aux", []))
        item["avg_subagent_calls"] = _avg(item.pop("_subcalls", []))
        item["avg_dispatch_rounds"] = _avg(item.pop("_rounds", []))
        scores = item.pop("_scores", [])
        item["avg_score"] = round(sum(scores) / len(scores), 2) if scores else None
    return out


def to_markdown(rows: list[dict], meta: dict) -> str:
    summary = summarize(rows)
    lines = [
        "# 派发 ROI 评测报告",
        "",
        f"- 时间：{meta.get('generated_at')}",
        f"- 后端：{meta.get('base_url')}",
        f"- 题数：{meta.get('limit') or len(rows)}（每题每配置一条运行）",
        f"- LLM 裁判：{'开' if meta.get('judge') else '关'}",
        "",
        "## 汇总",
        "",
        "| 配置 | 题数 | 平均延迟(ms) | 平均 tokens | 平均辅助 tokens | 平均子代理调用 | 有派发题数 | 平均派发轮数 | 非 ok | 平均得分 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for config, item in sorted(summary.items()):
        score = "-" if item["avg_score"] is None else item["avg_score"]
        lines.append(
            f"| {config} | {item['count']} | {item['avg_latency_ms']} | "
            f"{item['avg_total_tokens']} | {item['avg_aux_tokens']} | "
            f"{item['avg_subagent_calls']} | {item['dispatched_questions']} | "
            f"{item['avg_dispatch_rounds']} | {item['errors']} | {score} |"
        )
    lines += [
        "",
        "## 逐题",
        "",
        "| id | 类别 | 期望 ROI | 配置 | 状态 | 延迟(ms) | tokens | 子代理调用 | 派发子任务 | 得分 |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        score = "-" if row.get("score") is None else row["score"]
        lines.append(
            f"| {row.get('id')} | {row.get('category')} | {row.get('roi')} | "
            f"{row.get('config')} | {row.get('status')} | {row.get('latency_ms')} | "
            f"{row.get('total_tokens')} | {row.get('subagent_llm_calls')} | "
            f"{row.get('dispatch_subtasks')} | {score} |"
        )
    lines.append("")
    if len(summary) >= 2:
        on = summary.get("subagents-on")
        off = summary.get("subagents-off")
        if on and off:
            lines += [
                "## 对比结论（自动）",
                "",
                f"- 延迟：开 {on['avg_latency_ms']}ms vs 关 {off['avg_latency_ms']}ms",
                f"- tokens：开 {on['avg_total_tokens']} vs 关 {off['avg_total_tokens']}",
                f"- 有派发题数：开 {on['dispatched_questions']}/{on['count']} vs 关 {off['dispatched_questions']}/{off['count']}",
            ]
            if on["avg_score"] is not None and off["avg_score"] is not None:
                lines.append(
                    f"- 质量：开 {on['avg_score']} vs 关 {off['avg_score']}（满分 2）"
                )
            lines.append("")
    return "\n".join(lines)


def write_outputs(rows: list[dict], meta: dict, out: Path) -> tuple[Path, Path, Path]:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_markdown(rows, meta), encoding="utf-8")
    jsonl = out.with_suffix(".jsonl")
    with jsonl.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    csv_path = out.with_suffix(".csv")
    fields = [
        "config", "id", "category", "roi", "status", "latency_ms",
        "total_tokens", "aux_total_tokens", "subagent_llm_calls",
        "dispatch_rounds", "dispatch_subtasks", "subagent_ms", "score", "reason",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return out, jsonl, csv_path


class Api:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/") + "/api"
        self.client = httpx.Client(trust_env=False, timeout=600)

    def close(self) -> None:
        self.client.close()

    def _json(self, response: httpx.Response) -> dict:
        response.raise_for_status()
        return response.json()

    def subagents_enabled(self) -> bool:
        data = self._json(self.client.get(f"{self.base}/settings"))
        return bool((data.get("editable") or {}).get("agent_subagents_enabled"))

    def set_subagents_enabled(self, enabled: bool) -> None:
        self._json(
            self.client.put(
                f"{self.base}/settings",
                json={"updates": {"agent_subagents_enabled": bool(enabled)}},
            )
        )

    def create_conversation(self, title: str) -> int:
        data = self._json(
            self.client.post(f"{self.base}/conversations", json={"title": title})
        )
        return int(data["id"])

    def chat(self, conversation_id: int, question: str, use_kb: bool) -> dict:
        return self._json(
            self.client.post(
                f"{self.base}/agent/chat",
                json={
                    "question": question,
                    "conversation_id": conversation_id,
                    "use_web_search": False,
                    "use_knowledge_base": bool(use_kb),
                },
            )
        )

    def latest_run(self, conversation_id: int) -> dict:
        data = self._json(
            self.client.get(
                f"{self.base}/runs",
                params={"conversation_id": conversation_id, "limit": 1},
            )
        )
        return data[0] if data else {}

    def delete_conversation(self, conversation_id: int) -> None:
        self.client.delete(f"{self.base}/conversations/{conversation_id}")


def _load_judge():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from evaluate_agent import _load_judge_llm, judge_answer

        return _load_judge_llm(), judge_answer
    except Exception as exc:  # noqa: BLE001
        print(f"裁判模型不可用，跳过打分：{exc}")
        return None, None


def run_config(
    api: Api,
    questions: list[dict],
    config: str,
    judge,
    judge_answer,
    cleanup: bool,
) -> list[dict]:
    rows: list[dict] = []
    for index, question in enumerate(questions, 1):
        qid = str(question["id"])
        print(f"[{config}] {index}/{len(questions)} {qid} …", flush=True)
        conv_id = api.create_conversation(f"dispatch-eval:{config}:{qid}")
        started = time.perf_counter()
        try:
            response = api.chat(
                conv_id, str(question.get("question") or ""), bool(question.get("use_kb"))
            )
            run = api.latest_run(conv_id)
            metrics = extract_run_metrics(run)
            if not run:
                metrics["status"] = "no_run"
            answer = str(response.get("answer") or "")
            score = reason = None
            if judge is not None and judge_answer is not None:
                score, reason = judge_answer(
                    judge,
                    str(question.get("question") or ""),
                    str(question.get("expect") or ""),
                    answer,
                    response.get("plan") or [],
                    response.get("tool_trace") or [],
                )
            row = {
                "config": config,
                "id": qid,
                "category": question.get("category") or "",
                "roi": question.get("roi") or "",
                "score": score,
                "reason": reason or "",
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                **metrics,
            }
        except Exception as exc:  # noqa: BLE001 - 单题失败不终止整轮评测
            row = {
                "config": config,
                "id": qid,
                "category": question.get("category") or "",
                "roi": question.get("roi") or "",
                "status": f"error:{type(exc).__name__}",
                "error": str(exc)[:300],
                "score": None,
                "reason": "",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "total_tokens": 0,
                "aux_total_tokens": 0,
                "subagent_llm_calls": 0,
                "dispatch_rounds": 0,
                "dispatch_subtasks": 0,
                "subagent_ms": 0,
            }
            print(f"  失败：{exc}", flush=True)
        finally:
            if cleanup:
                try:
                    api.delete_conversation(conv_id)
                except Exception as exc:  # noqa: BLE001
                    print(f"  清理会话 {conv_id} 失败：{exc}", flush=True)
        rows.append(row)
        print(
            f"  {row.get('status')} · {row.get('latency_ms')}ms · "
            f"{row.get('total_tokens')} tokens · 子代理 {row.get('subagent_llm_calls')} 次 · "
            f"派发 {row.get('dispatch_subtasks')} 个",
            flush=True,
        )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全部）")
    ap.add_argument("--category", default="", help="按 category 过滤")
    ap.add_argument("--compare", action="store_true", help="跑子代理开/关两种配置")
    ap.add_argument("--judge", action="store_true", help="启用 LLM 裁判打分（更慢/有费用）")
    ap.add_argument("--cleanup", action="store_true", help="结束后删除本脚本创建的会话")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    questions = load_questions(args.questions)
    if args.category:
        questions = [q for q in questions if q.get("category") == args.category]
    if args.limit and args.limit > 0:
        questions = questions[: args.limit]
    if not questions:
        raise SystemExit("没有可跑的题目")

    judge = judge_answer = None
    if args.judge:
        judge, judge_answer = _load_judge()

    api = Api(args.base_url)
    original = None
    rows: list[dict] = []
    try:
        original = api.subagents_enabled()
        if args.compare:
            configs = [("subagents-on", True), ("subagents-off", False)]
        else:
            configs = [("current", original)]
        for config, enabled in configs:
            api.set_subagents_enabled(enabled)
            print(f"== 配置 {config}（agent_subagents_enabled={enabled}）==", flush=True)
            rows.extend(
                run_config(api, questions, config, judge, judge_answer, args.cleanup)
            )
    finally:
        if original is not None:
            try:
                api.set_subagents_enabled(original)
                print(f"已恢复 agent_subagents_enabled={original}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"恢复设置失败（请手动检查）：{exc}", flush=True)
        api.close()

    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_url": args.base_url,
        "limit": len(questions),
        "judge": bool(judge),
    }
    out, jsonl, csv_path = write_outputs(rows, meta, args.out)
    print(f"\n报告：{out}\n明细：{jsonl}\nCSV：{csv_path}")
    summary = summarize(rows)
    for config, item in sorted(summary.items()):
        print(
            f"{config}: n={item['count']} 平均延迟 {item['avg_latency_ms']}ms "
            f"平均 tokens {item['avg_total_tokens']} 子代理调用 {item['avg_subagent_calls']} "
            f"派发题 {item['dispatched_questions']}/{item['count']}"
        )


if __name__ == "__main__":
    main()
