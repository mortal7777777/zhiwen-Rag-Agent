"""轻量 Agent 评测脚本：按 eval_questions.json 逐题调用 /api/agent/stream，
记录延迟/工具调用/计划/答案长度/运行状态，输出 eval_report.jsonl 与汇总表。

用法（backend 目录）：
    python evaluate_agent.py [--limit N] [--category kb]

说明：
- 每题会创建一次新会话（作为评测留痕），可在前端“运行记录”里查看；
- 结果只做客观记录，答案质量需人工抽查（或后续接 RAGAS 做自动指标）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "http://127.0.0.1:8000/api"
REPORT = Path(__file__).resolve().parent / "eval_report.jsonl"


def _post(path: str, payload: dict, method: str = "POST"):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def stream_question(
    question: str,
    tool_mode: str,
    conversation_id=None,
) -> dict:
    req = urllib.request.Request(
        BASE + "/agent/stream",
        data=json.dumps(
            {
                "question": question,
                "conversation_id": conversation_id,
                "tool_mode": tool_mode,
                "template_id": None,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    events = []
    conv_id = None
    plan: list[str] = []
    tools: list[dict] = []
    error = None
    with urllib.request.urlopen(req, timeout=300) as resp:
        buf = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                for line in frame.decode("utf-8", "replace").splitlines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        payload = json.loads(line[6:])
                        events.append(payload)
                    except json.JSONDecodeError:
                        continue
    for ev in events:
        e, d = ev.get("event"), ev.get("data") or {}
        if e == "session":
            conv_id = d.get("conversation_id")
        elif e == "plan":
            plan = d.get("steps") or []
        elif e == "tool_start":
            tools.append({"name": d.get("name"), "args": d.get("arguments") or {}})
        elif e == "error":
            error = d.get("message") or str(d)
    latency_ms = round((time.time() - t0) * 1000)
    run = None
    if conv_id:
        runs = _get(f"/runs?conversation_id={conv_id}")
        run = runs[0] if runs else None
    return {
        "conversation_id": conv_id,
        "latency_ms": latency_ms,
        "plan": plan,
        "tools": tools,
        "error": error,
        "run": run,
    }
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--category", default="")
    parser.add_argument("--approve", action="store_true", help="自动批准敏感操作（仅评测用）")
    args = parser.parse_args()
    if args.approve:
        # 评测模式：临时切到“自动批准”，跑完恢复“每次确认”
        _post("/settings", {"updates": {"tool_permission_mode": "allow"}}, method="PUT")
        print("已临时切换 tool_permission_mode=allow", flush=True)
    questions = json.loads(
        (Path(__file__).resolve().parent / "eval_questions.json").read_text(
            encoding="utf-8"
        )
    )
    if args.category:
        questions = [q for q in questions if q["category"] == args.category]
    if args.limit:
        questions = questions[: args.limit]

    print(f"评测 {len(questions)} 题，写入 {REPORT.name}\n")
    rows = []
    sessions: dict[str, int] = {}
    for q in questions:
        qid = q["id"]
        print(
            f"[{qid}] L{q.get('difficulty', '-')} {q['question'][:36]}…",
            flush=True,
        )
        try:
            info = stream_question(
                q["question"],
                q["tool_mode"],
                conversation_id=sessions.get(q.get("session") or ""),
            )
        except Exception as exc:
            info = {"error": str(exc)}
            print(f"  !! 请求失败：{exc}", flush=True)
        if q.get("session") and info.get("conversation_id"):
            sessions[q["session"]] = info["conversation_id"]
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "id": qid,
            "difficulty": q.get("difficulty"),
            "category": q["category"],
            "question": q["question"],
            "expect": q.get("expect", ""),
            **info,
        }
        if row.get("run"):
            run = row["run"]
            row["run_status"] = run.get("status")
            row["run_error"] = run.get("error")
            row["answer_len"] = run.get("answer_len")
            row["token_usage"] = run.get("token_usage")
        rows.append(row)
        with REPORT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(
            f"  {row.get('latency_ms', 0) / 1000:.1f}s | "
            f"tools={len(row.get('tools') or [])} | "
            f"plan={len(row.get('plan') or [])}步 | "
            f"status={row.get('run_status', '-')} | "
            f"answer_len={row.get('answer_len', '-')}",
            flush=True,
        )
    if args.approve:
        _post("/settings", {"updates": {"tool_permission_mode": "ask"}}, method="PUT")
        print("已恢复 tool_permission_mode=ask", flush=True)
    print("\n完成，报告：", REPORT)


if __name__ == "__main__":
    main()
