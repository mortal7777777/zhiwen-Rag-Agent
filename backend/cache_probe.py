"""缓存命中率探针：历史 run 级统计 + 多轮长 coding 任务 e2e 实测。

用法（backend 目录；需后端与 OpenSearch 已启动）：
    python cache_probe.py stats [--days 7] [--limit 3000]
    python cache_probe.py coding [--turns 4] [--keep-settings]
    python cache_probe.py realistic [--scenarios kb,project] [--provider deepseek] [--keep-settings]

realistic 模式：贴合真实使用的两组会话
- kb      ：知识库多轮追问（两本书对比、表格、写作、延伸；书名用
            PROBE_BOOK_A / PROBE_BOOK_B 环境变量指定，默认占位名），长间隔检索轮；
- project ：项目工作（读 context.py / nodes/agent.py、写脚本统计行数并运行），
            短间隔工具轮；工作目录=仓库根，写文件限定在 .cache_probe_tmp/。
provider 传供应商 id（"deepseek"=OpenAI 格式，"p_1789054147715"=Anthropic 格式），
省略=用当前激活供应商；测完恢复原值。

口径（与 INTERVIEW 文档一致）：
- run 级 = 该 run 主循环全部调用 read/(read+miss)（token_usage.cache_hit_tokens / cache_miss_tokens）
- 逐调用 = calls[i].read / calls[i].in；c1 为每个 run 的首次调用
- 逐调用明细来自 agent_runs.token_usage.calls（tracing.py 遥测）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE = "http://127.0.0.1:8000/api"


# ---------------- 通用 ----------------

def _load_db():
    from run import load_local_env

    load_local_env()
    import app.db.database as db_mod

    db_mod.init_db()
    return db_mod


def _fetch_runs(limit: int = 3000) -> list[dict]:
    """从 MySQL 拉取 agent_runs（新→旧），解析 token_usage。"""
    db_mod = _load_db()
    from app.db import models

    s = db_mod.SessionLocal()
    try:
        rows = (
            s.query(models.AgentRun)
            .order_by(models.AgentRun.id.desc())
            .limit(limit)
            .all()
        )
        out = []
        for r in rows:
            usage = {}
            try:
                usage = json.loads(r.token_usage or "{}")
            except Exception:
                pass
            out.append(
                {
                    "id": r.id,
                    "conv": r.conversation_id,
                    "status": r.status,
                    "created": r.created_at,
                    "usage": usage,
                    "q": (r.question or "")[:50],
                }
            )
        return out
    finally:
        s.close()


def _run_rate(usage: dict) -> float | None:
    hit = int(usage.get("cache_hit_tokens") or 0)
    miss = int(usage.get("cache_miss_tokens") or 0)
    return hit / (hit + miss) if (hit + miss) > 0 else None


def _pct(v) -> str:
    return "  n/a" if v is None else f"{v * 100:5.1f}%"


def _agg(rates: list[float]) -> str:
    if not rates:
        return "n/a"
    rates = sorted(rates)
    n = len(rates)
    med = rates[n // 2] if n % 2 else (rates[n // 2 - 1] + rates[n // 2]) / 2
    p25 = rates[int(n * 0.25)]
    p75 = rates[int(n * 0.75)]
    mean = sum(rates) / n
    return f"n={n} mean={mean*100:.1f}% med={med*100:.1f}% p25={p25*100:.1f}% p75={p75*100:.1f}% min={rates[0]*100:.0f}% max={rates[-1]*100:.0f}%"


def _pooled(runs: list[dict]) -> tuple[float | None, int, int]:
    hit = sum(int(r["usage"].get("cache_hit_tokens") or 0) for r in runs)
    miss = sum(int(r["usage"].get("cache_miss_tokens") or 0) for r in runs)
    return (hit / (hit + miss) if (hit + miss) else None), hit, miss


# ---------------- stats：历史 run 级 ----------------

def stats(days: int, limit: int) -> None:
    runs = _fetch_runs(limit)
    valid = [r for r in runs if _run_rate(r["usage"]) is not None]
    print(f"agent_runs 总数（本次读取）：{len(runs)}，有效缓存样本：{len(valid)}")
    if not valid:
        return
    now = datetime.now()

    def _show(name: str, subset: list[dict]) -> None:
        if not subset:
            print(f"\n== {name} ==\n（无样本）")
            return
        overall, hit, miss = _pooled(subset)
        rates = [_run_rate(r["usage"]) for r in subset]
        print(f"\n== {name} ==")
        print(f"  pooled 口径（Σhit/Σ(hit+miss)）: {_pct(overall)}  (hit={hit:,} miss={miss:,})")
        print(f"  逐 run 分布: {_agg(rates)}")

    _show(f"全量（近 {len(valid)} 个 run）", valid)
    recent = [r for r in valid if r["created"] and r["created"] >= now - timedelta(days=days)]
    _show(f"近 {days} 天", recent)

    # 多轮会话（同一会话 ≥3 个 run）
    by_conv: dict[int, list[dict]] = {}
    for r in valid:
        if r["conv"]:
            by_conv.setdefault(r["conv"], []).append(r)
    multi = [rs for rs in by_conv.values() if len(rs) >= 3]
    if multi:
        flat = [r for rs in multi for r in rs]
        _show(f"多轮会话（{len(multi)} 个会话、共 {len(flat)} 个 run）", flat)
        print("  各多轮会话明细（新→旧）：")
        for rs in sorted(multi, key=lambda x: -x[0]["id"])[:12]:
            overall, hit, miss = _pooled(rs)
            print(
                f"    conv {rs[0]['conv']}: {len(rs)} runs, pooled {_pct(overall)} "
                f"(最新: {rs[0]['q']} …)"
            )

    # 逐调用：全量样本的 c1 vs c2+，及按前序调用耗时分桶
    c1: list[float] = []
    c2p: list[float] = []
    buckets = {"<5s": [], "5-20s": [], ">20s": []}
    for r in valid:
        calls = r["usage"].get("calls") or []
        prev_ms = None
        for i, c in enumerate(calls):
            tin = int(c.get("in") or 0)
            read = int(c.get("read") or 0)
            if tin <= 0:
                continue
            rate = read / tin
            if i == 0:
                c1.append(rate)
            else:
                c2p.append(rate)
                if prev_ms is not None:
                    key = "<5s" if prev_ms < 5000 else ("5-20s" if prev_ms < 20000 else ">20s")
                    buckets[key].append(rate)
            prev_ms = c.get("t_ms")
    if c1 or c2p:
        print("\n== 逐调用口径（read/in）==")
        print(f"  c1（每 run 首调）: {_agg(c1)}")
        print(f"  c2+（轮内后续）  : {_agg(c2p)}")
        for k, v in buckets.items():
            print(f"  前序调用耗时 {k:>6}: {_agg(v)}")


# ---------------- coding：多轮 e2e ----------------

def _get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _put(path: str, payload: dict):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _find_key(obj, key, default=None):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            got = _find_key(v, key, None)
            if got is not None:
                return got
    return default


def _scratch_dir() -> str:
    """编码测试的独立工作目录（避免把测试文件写进项目根）。"""
    import os

    root = os.environ.get("TEMP") or os.environ.get("TMP") or "C:/Temp"
    d = Path(root) / "zhiwen_cache_test"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _stream(
    question: str,
    conversation_id: int | None,
    timeout: float = 600.0,
    use_kb: bool = False,
    project_dir: str | None = None,
) -> dict:
    """POST /agent/stream（SSE），复用 evaluate_agent.py 的解析方式。"""
    req = urllib.request.Request(
        BASE + "/agent/stream",
        data=json.dumps(
            {
                "question": question,
                "conversation_id": conversation_id,
                "tool_mode": "auto",
                "use_web_search": False,
                "use_knowledge_base": use_kb,
                "project_dir": project_dir or _scratch_dir(),
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    events = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        buf = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                for line in frame.decode("utf-8", "replace").splitlines():
                    if line.startswith("data: "):
                        try:
                            events.append(json.loads(line[6:]))
                        except json.JSONDecodeError:
                            pass
    conv_id, tools, error, answer = None, [], None, []
    for ev in events:
        e, d = ev.get("event"), ev.get("data") or {}
        if e == "session":
            conv_id = d.get("conversation_id")
        elif e == "tool_start":
            tools.append(d.get("name"))
        elif e == "token":
            answer.append(d if isinstance(d, str) else str(d))
        elif e == "error":
            error = d.get("message") or str(d)
    return {
        "conversation_id": conv_id,
        "latency_s": round(time.time() - t0, 1),
        "tools": tools,
        "error": error,
        "answer": "".join(answer),
    }


def _latest_run(conv_id: int) -> dict | None:
    db_mod = _load_db()
    from app.db import models

    s = db_mod.SessionLocal()
    try:
        r = (
            s.query(models.AgentRun)
            .filter(models.AgentRun.conversation_id == conv_id)
            .order_by(models.AgentRun.id.desc())
            .first()
        )
        if r is None:
            return None
        usage = {}
        try:
            usage = json.loads(r.token_usage or "{}")
        except Exception:
            pass
        return {"id": r.id, "usage": usage, "status": r.status}
    finally:
        s.close()


CODING_PROMPTS = [
    "在当前工作目录创建一个 Python 项目 fizzbuzz：写 fizzbuzz.py，实现 fizzbuzz(n, rules) 函数"
    "（rules 是 [(除数, 替换词)] 列表，默认经典 FizzBuzz 规则）；写 test_fizzbuzz.py 覆盖基本用例；"
    "运行测试确认全部通过。",
    "重构 fizzbuzz.py：对 rules 做健壮性校验（除数必须为正整数、规则列表非空），补充对应测试用例，"
    "然后重新运行测试确认通过。",
    "给项目写 README.md（用法、示例、如何运行测试），然后在 README 中补一节：列出当前文件清单和各文件代码行数"
    "（用命令统计后写入）。",
    "做最后一次代码审查：检查 fizzbuzz.py 和测试文件，修复你发现的问题（如果有），重新运行测试，"
    "最后用一句话总结项目状态。",
]


def coding(turns: int, keep_settings: bool) -> None:
    prompts = CODING_PROMPTS[:turns]
    # 临时 allow，避免 coding 的写文件/命令卡在人工审批
    old_mode = None
    try:
        view = _get("/settings")
        old_mode = _find_key(view, "tool_permission_mode", "ask")
        _put("/settings", {"updates": {"tool_permission_mode": "allow"}})
        print(f"审批模式 {old_mode} → allow（测试结束后恢复）", flush=True)
    except Exception as exc:
        print(f"!! 切换审批模式失败（将可能卡在审批）：{exc}")
        old_mode = None

    conv_id = None
    per_turn: list[dict] = []
    try:
        for i, q in enumerate(prompts, 1):
            print(f"\n===== 第 {i} 轮（conv={conv_id}）=====", flush=True)
            print(f"Q: {q[:60]}…", flush=True)
            try:
                res = _stream(q, conv_id)
                conv_id = res["conversation_id"] or conv_id
                print(
                    f"  完成：{res['latency_s']}s，工具 {len(res['tools'])} 次"
                    f"（{', '.join(res['tools'][:8])}）",
                    flush=True,
                )
            except Exception as exc:
                print(f"  !! 请求失败：{exc}", flush=True)
                break
            run = _latest_run(conv_id) if conv_id else None
            if not run:
                print("  !! 未取到 run 记录", flush=True)
                continue
            usage = run["usage"]
            calls = usage.get("calls") or []
            rate = _run_rate(usage)
            print(f"  run #{run['id']} 状态={run['status']} run级命中={_pct(rate)}", flush=True)
            for j, c in enumerate(calls, 1):
                tin = int(c.get("in") or 0)
                read = int(c.get("read") or 0)
                r = read / tin if tin else 0.0
                print(
                    f"    c{j}: in={tin:>6,} read={read:>6,} → {r*100:5.1f}%"
                    f"  ({c.get('t_ms') or 0}ms)",
                    flush=True,
                )
            per_turn.append({"turn": i, "run": run, "rate": rate, "calls": calls})
    finally:
        if old_mode and not keep_settings:
            try:
                _put("/settings", {"updates": {"tool_permission_mode": old_mode}})
                print(f"\n审批模式已恢复为 {old_mode}")
            except Exception as exc:
                print(f"\n!! 恢复审批模式失败：{exc}")

    if per_turn:
        hit = sum(int(t["run"]["usage"].get("cache_hit_tokens") or 0) for t in per_turn)
        miss = sum(int(t["run"]["usage"].get("cache_miss_tokens") or 0) for t in per_turn)
        overall = hit / (hit + miss) if (hit + miss) else None
        c1 = []
        c2p = []
        for t in per_turn:
            for i, c in enumerate(t["calls"]):
                tin = int(c.get("in") or 0)
                if tin <= 0:
                    continue
                (c1 if i == 0 else c2p).append(int(c.get("read") or 0) / tin)
        print("\n===== 会话汇总 =====")
        print(f"  共 {len(per_turn)} 轮；会话 run 级 pooled 命中：{_pct(overall)}  (hit={hit:,} miss={miss:,})")
        if c1:
            print(f"  各轮首调 c1：{[round(x*100) for x in c1]} %")
        if c2p:
            print(f"  轮内 c2+   ：{_agg(c2p)}")


# ---------------- realistic：真实使用场景 e2e ----------------

REPO_ROOT = str(Path(__file__).resolve().parents[1])  # rag_knowledge_base/

# 书名从环境变量取（默认占位名）：公开仓库不放私人书目，
# 本机跑真实场景时用 PROBE_BOOK_A / PROBE_BOOK_B 指定即可
BOOK_A = os.environ.get("PROBE_BOOK_A", "书名A")
BOOK_B = os.environ.get("PROBE_BOOK_B", "书名B")

KB_PROMPTS = [
    f"《{BOOK_A}》这套书一共几本？整套书主要在讲什么？",
    f"那《{BOOK_B}》呢？它和《{BOOK_A}》的核心观点有什么不一样？",
    "把这两本书在这些核心议题上的差异整理成一个表格，每条尽量注明出处。",
    "我要在读书会上做一次分享，帮我写一段 300 字左右的开场白，把这几个差异点串起来。",
    "分享的时候听众可能会问什么？帮我列 5 个问题，每个附一句简短的回答要点。",
]

PROJECT_PROMPTS = [
    "阅读 backend/app/agent/context.py，说明上下文压缩窗口的触发条件和窗口起点是怎么确定的。",
    "再看一下 app/agent/nodes/agent.py 里主循环是怎么用这个窗口的，两者怎么配合？",
    "写一个脚本统计 backend/app 下所有 .py 文件的行数（按降序），存成 JSON，然后运行它。"
    "脚本和输出放到工作目录下的 .cache_probe_tmp/ 里。",
    "根据运行结果，列出最长的 3 个文件，各用一句话说明它们负责什么。",
]


def _run_prompts(label: str, prompts: list[str], *, use_kb: bool, project_dir: str) -> list[dict]:
    """在同一会话里依次提问，逐轮打印 run 级与逐调用命中。"""
    conv_id = None
    per_turn: list[dict] = []
    for i, q in enumerate(prompts, 1):
        print(f"\n----- [{label}] 第 {i}/{len(prompts)} 轮（conv={conv_id}）-----", flush=True)
        print(f"Q: {q[:70]}…", flush=True)
        try:
            res = _stream(q, conv_id, use_kb=use_kb, project_dir=project_dir)
            conv_id = res["conversation_id"] or conv_id
            print(
                f"  完成：{res['latency_s']}s，工具 {len(res['tools'])} 次"
                f"（{', '.join(res['tools'][:8])}）",
                flush=True,
            )
        except Exception as exc:
            print(f"  !! 请求失败：{exc}", flush=True)
            break
        run = _latest_run(conv_id) if conv_id else None
        if not run:
            print("  !! 未取到 run 记录", flush=True)
            continue
        usage = run["usage"]
        calls = usage.get("calls") or []
        print(f"  run #{run['id']} 状态={run['status']} run级命中={_pct(_run_rate(usage))}", flush=True)
        for j, c in enumerate(calls, 1):
            tin = int(c.get("in") or 0)
            read = int(c.get("read") or 0)
            r = read / tin if tin else 0.0
            print(f"    c{j}: in={tin:>6,} read={read:>6,} → {r*100:5.1f}%  ({c.get('t_ms') or 0}ms)", flush=True)
        per_turn.append({"turn": i, "run": run, "rate": _run_rate(usage), "calls": calls})
    return per_turn


def _summarize(label: str, per_turn: list[dict]) -> None:
    if not per_turn:
        print(f"\n[{label}] 无有效轮次")
        return
    hit = sum(int(t["run"]["usage"].get("cache_hit_tokens") or 0) for t in per_turn)
    miss = sum(int(t["run"]["usage"].get("cache_miss_tokens") or 0) for t in per_turn)
    overall = hit / (hit + miss) if (hit + miss) else None
    c1, c2p = [], []
    for t in per_turn:
        for i, c in enumerate(t["calls"]):
            tin = int(c.get("in") or 0)
            if tin <= 0:
                continue
            (c1 if i == 0 else c2p).append(int(c.get("read") or 0) / tin)
    print(f"\n===== [{label}] 会话汇总 =====")
    print(f"  {len(per_turn)} 轮；run 级 pooled：{_pct(overall)}  (hit={hit:,} miss={miss:,})")
    print(f"  逐 run：{[round((t['rate'] or 0) * 100) for t in per_turn]} %")
    if c1:
        print(f"  各轮首调 c1：{[round(x * 100) for x in c1]} %")
    if c2p:
        print(f"  轮内 c2+   ：{_agg(c2p)}")


def realistic(scenarios: str, provider: str | None, keep_settings: bool) -> None:
    names = [s.strip() for s in scenarios.split(",") if s.strip()]
    old_mode = old_provider = None
    try:
        view = _get("/settings")
        old_mode = _find_key(view, "tool_permission_mode", "ask")
        old_provider = _find_key(view, "active_chat_provider", None)
        upd: dict = {"tool_permission_mode": "allow"}
        if provider:
            upd["active_chat_provider"] = provider
        _put("/settings", {"updates": upd})
        print(
            f"审批模式 {old_mode} → allow；供应商 {old_provider} → {provider or old_provider}（测后恢复）",
            flush=True,
        )
    except Exception as exc:
        print(f"!! 切换设置失败（可能卡审批）：{exc}", flush=True)

    tmp_dir = Path(REPO_ROOT) / ".cache_probe_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    kb_turns: list[dict] = []
    project_turns: list[dict] = []
    try:
        if "kb" in names:
            kb_turns = _run_prompts("KB", KB_PROMPTS, use_kb=True, project_dir=_scratch_dir())
            _summarize("KB", kb_turns)
        if "project" in names:
            project_turns = _run_prompts("PROJECT", PROJECT_PROMPTS, use_kb=False, project_dir=REPO_ROOT)
            _summarize("PROJECT", project_turns)
    finally:
        if not keep_settings:
            upd = {"tool_permission_mode": old_mode} if old_mode else {}
            if provider and old_provider:
                upd["active_chat_provider"] = old_provider
            if upd:
                try:
                    _put("/settings", {"updates": upd})
                    print(f"\n设置已恢复（permission={old_mode}, provider={old_provider}）")
                except Exception as exc:
                    print(f"\n!! 恢复设置失败：{exc}")
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)

    all_turns = kb_turns + project_turns
    if len(names) > 1 and all_turns:
        _summarize("合计", all_turns)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    p1 = sub.add_parser("stats")
    p1.add_argument("--days", type=int, default=7)
    p1.add_argument("--limit", type=int, default=3000)
    p2 = sub.add_parser("coding")
    p2.add_argument("--turns", type=int, default=4)
    p2.add_argument("--keep-settings", action="store_true")
    p3 = sub.add_parser("realistic")
    p3.add_argument("--scenarios", default="kb,project")
    p3.add_argument("--provider", default=None, help="供应商 id；省略=当前激活")
    p3.add_argument("--keep-settings", action="store_true")
    args = parser.parse_args()
    if args.mode == "stats":
        stats(args.days, args.limit)
    elif args.mode == "coding":
        coding(args.turns, args.keep_settings)
    else:
        realistic(args.scenarios, args.provider, args.keep_settings)


if __name__ == "__main__":
    main()
