"""轻量 Agent 评测脚本：按 eval_questions.json 逐题调用 /api/agent/stream，
记录延迟/工具调用/计划/答案长度/运行状态，并用 LLM-as-judge 自动打分。

用法（backend 目录）：
    python evaluate_agent.py [--limit N] [--category kb] [--no-judge]

说明：
- 每题会创建一次新会话（作为评测留痕），可在前端“运行记录”里查看；
- judge 用低温裁判模型按 expect 字段（rubric）逐题打 0/1/2 分并给理由，
  写入 eval_report.jsonl；之后可用 assert_eval_report.py 做过程断言。
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

# judge 评分 rubric：0=明显失败 1=基本完成有缺陷 2=完全符合 expect
JUDGE_RUBRIC = """评分标准：
- 2 分：完全符合期望——任务完成，事实正确，按要求给出引用/来源/说明；
- 1 分：基本完成但有缺陷——信息不完整、个别事实错误、未按要求引用或标注、步骤遗漏；
- 0 分：明显失败——答非所问、关键事实错误、任务未完成、拒绝作答、输出泄漏工具调用标记。

只输出 JSON：{"score": 0|1|2, "reason": "一句话理由，指出做对与做错的地方"}"""


def _load_judge_llm():
    """裁判模型：当前对话供应商 + 低温 + 关闭思考（与标题模型同策略）。

    评测脚本是独立进程,必须先从 MySQL 加载设置页保存的覆盖值
    (load_overrides),否则 chat_provider_config 只会读到环境变量,
    设置页换过 key 后会拿旧 key 打评测,报 402 Insufficient Balance。
    """
    from langchain_openai import ChatOpenAI

    from app.config import get_settings
    from app.runtime_config import (
        chat_provider_config,
        load_overrides,
        thinking_extra_body,
    )

    try:
        from run import load_local_env

        load_local_env()  # 独立进程先加载 backend/.env.local(MYSQL_URL 等)
        import app.db.database as db_mod

        db_mod.init_db()  # 初始化数据库连接(SessionLocal 由 init_db 赋值)
        if db_mod.SessionLocal is not None:
            s = db_mod.SessionLocal()
            try:
                load_overrides(s)
            finally:
                s.close()
    except Exception:
        pass
    cfg = chat_provider_config(get_settings()) or {}
    if not cfg.get("api_key"):
        raise RuntimeError("未配置对话模型 API Key，无法启动 judge（可用 --no-judge 跳过）")
    return ChatOpenAI(
        api_key=cfg.get("api_key"),
        base_url=cfg.get("base_url") or "https://api.deepseek.com",
        model=cfg.get("model") or "deepseek-v4-flash",
        temperature=0.0,
        request_timeout=120,
        max_retries=1,
        extra_body=thinking_extra_body({"thinking_enabled": False}),
    )


def judge_answer(
    judge,
    question: str,
    expect: str,
    answer: str,
    plan: list,
    tools: list,
) -> tuple[int | None, str]:
    """LLM-as-judge：按 expect rubric 打分，返回 (score, reason)，失败返回 (None, "")。"""
    tool_summary = "; ".join(
        f"{t.get('name')}" for t in (tools or [])[:10]
    ) or "（无工具调用）"
    prompt = f"""你是严格的 Agent 评测裁判。根据期望（expect）评判回答质量。

问题：{question[:500]}

期望（expect，即评分依据）：{expect[:500]}

Agent 执行过程：计划 {len(plan or [])} 步，工具调用：{tool_summary}

回答：
{str(answer or '')[:2500]}

{JUDGE_RUBRIC}"""
    try:
        resp = judge.invoke([{"role": "user", "content": prompt}])
        text = (resp.content or "").strip()
        import re

        match = re.search(r"\{.*\}", text, re.DOTALL)
        data = json.loads(match.group(0)) if match else {}
        score = int(data.get("score", -1))
        reason = str(data.get("reason", ""))[:300]
        if score in (0, 1, 2):
            return score, reason
        return None, f"judge 输出无法解析：{text[:120]}"
    except Exception as exc:
        return None, f"judge 调用失败：{str(exc)[:120]}"


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
    answer_parts: list[str] = []
    sources: list[dict] = []
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
        elif e == "token":
            answer_parts.append(d if isinstance(d, str) else str(d))
        elif e == "done":
            sources = d.get("sources") or []
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
        "answer": "".join(answer_parts),
        "sources": sources,
        "run": run,
    }
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--category", default="")
    parser.add_argument("--approve", action="store_true", help="自动批准敏感操作（仅评测用）")
    parser.add_argument("--no-judge", action="store_true", help="跳过 LLM-as-judge 打分")
    args = parser.parse_args()
    if args.approve:
        # 评测模式：临时切到“自动批准”，跑完恢复“每次确认”
        _post("/settings", {"updates": {"tool_permission_mode": "allow"}}, method="PUT")
        print("已临时切换 tool_permission_mode=allow", flush=True)
    # 优先本地题库（含知识库书目等个人内容，gitignored）；不存在才用公开题库
    qpath = Path(__file__).resolve().parent / "eval_questions.local.json"
    if not qpath.exists():
        qpath = Path(__file__).resolve().parent / "eval_questions.json"
    questions = json.loads(qpath.read_text(encoding="utf-8"))
    if args.category:
        questions = [q for q in questions if q["category"] == args.category]
    if args.limit:
        questions = questions[: args.limit]

    print(f"评测 {len(questions)} 题，写入 {REPORT.name}\n")
    judge = None
    if not args.no_judge:
        try:
            judge = _load_judge_llm()
            print("LLM-as-judge 已就绪（每轮回答后自动打分 0/1/2）\n")
        except Exception as exc:
            print(f"!! judge 不可用（将跳过打分）：{exc}\n")
            judge = None
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
        if judge is not None:
            score, reason = judge_answer(
                judge,
                q["question"],
                q.get("expect", ""),
                row.get("answer") or "",
                row.get("plan") or [],
                row.get("tools") or [],
            )
            row["judge_score"] = score
            row["judge_reason"] = reason
        rows.append(row)
        with REPORT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        score_text = (
            f" | judge={row.get('judge_score', '-')}"
            if "judge_score" in row
            else ""
        )
        print(
            f"  {row.get('latency_ms', 0) / 1000:.1f}s | "
            f"tools={len(row.get('tools') or [])} | "
            f"plan={len(row.get('plan') or [])}步 | "
            f"status={row.get('run_status', '-')} | "
            f"answer_len={row.get('answer_len', '-')}"
            f"{score_text}",
            flush=True,
        )
    if args.approve:
        _post("/settings", {"updates": {"tool_permission_mode": "ask"}}, method="PUT")
        print("已恢复 tool_permission_mode=ask", flush=True)
    print("\n完成，报告：", REPORT)
    scored = [r for r in rows if r.get("judge_score") is not None]
    if scored:
        avg = sum(r["judge_score"] for r in scored) / len(scored)
        print(f"\nLLM-as-judge 汇总：{len(scored)} 题有效，平均 {avg:.2f} / 2")
        by_cat: dict[str, list[int]] = {}
        for r in scored:
            by_cat.setdefault(r["category"], []).append(r["judge_score"])
        for cat, scores in sorted(by_cat.items()):
            print(
                f"  {cat:<10} n={len(scores):>2}  avg={sum(scores) / len(scores):.2f}"
                f"  分布={scores.count(2)}×2 / {scores.count(1)}×1 / {scores.count(0)}×0"
            )


if __name__ == "__main__":
    main()
