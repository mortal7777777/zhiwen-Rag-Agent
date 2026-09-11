"""RAGAS 基线评测：检索 + 生成双维度指标。

用法（backend 目录）：
    python eval_ragas.py                      # 全量，检索结果默认复用缓存
    python eval_ragas.py --limit 3
    python eval_ragas.py --id l1-kb-001,l2-kb-009
    python eval_ragas.py --refresh            # 忽略检索缓存，重跑 /api/chat
    python eval_ragas.py --resume             # 续跑上次中断的 run，跳过已完成题
    python eval_ragas.py --tag 2026-09-11     # 结果确认为新基线，写入 ragas_baselines.json
    python eval_ragas.py --explain l2-kb-011  # 诊断：collections 新 API 打印裁判逐条判定理由

断点与缓存（2026-09-11 加，针对 judge 卡死导致的重复检索）：
- 检索结果逐题写 ragas_retrieval_cache.json（题干或管道指纹变化自动失效），
  重跑默认复用、不再重复调 /api/chat；--refresh 强制重跑；
- 管道指纹 = 检索参数 + 索引物理名 + 嵌入/重排 provider：改了检索逻辑/参数/
  重建索引后旧条目自动失效，不会"测了个寂寞"；
- 评测改为逐题 evaluate 并立即追加写 ragas_baseline.jsonl（行内带 run_id），
  中断只损失当前一题；--resume 接着跑，已完成题（faithfulness 非空）不再重复；
- --tag 把本次均值 + 逐题值写入 ragas_baselines.json（rails），供
  scripts/regression_gate.py --baseline <tag> 做相对基线回归。
- 题库可用 "skip_reference_metrics": true 声明只测 faithfulness（拒答类题）；
  "skip_faithfulness": true 则全部指标停测（S5 门槛后拒答题无上下文）。

指标（按可用数据自动组合）：
- Faithfulness        生成是否忠于检索上下文（无需参考答案，必测）；
- AnswerCorrectness   回答与参考答案的事实一致性（题目带 reference 时测）；
- ContextPrecision    检索上下文与参考答案相关度（带 reference 时测）；
- ContextRecall       参考答案被检索上下文覆盖的比例（带 reference 时测）。

说明：
- 逐题调用 /api/chat（RAG 管道），把来源内容作为 retrieved_contexts；
- 题库优先读本地 eval_questions.local.json（含知识库书目与参考答案，
  gitignored）；不存在才用公开版；
- 结果写入 ragas_baseline.jsonl，可在改动前后对比回归。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 关闭 ragas 遥测上报：其 requests.post 会走系统代理（Clash）卡住每次 judge
# 调用（实测卡死 0/84）；注意 ragas 只认字面量 "true"，"1" 不生效
os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")

from openai import OpenAI
from ragas import EvaluationDataset, SingleTurnSample, evaluate
from ragas.embeddings.base import Embeddings
from ragas.llms import llm_factory
from ragas.run_config import RunConfig
# 注意:必须用 ragas.metrics(legacy)而非 ragas.metrics.collections——
# 0.4.x 的 evaluate() 校验 isinstance(m, Metric),collections 里是 BaseMetric,
# 会报 "All metrics must be initialised metric objects"(有 DeprecationWarning,无害)
from ragas.metrics import (
    AnswerCorrectness,
    ContextPrecision,
    ContextRecall,
    Faithfulness,
)

BASE = "http://127.0.0.1:8000/api"
REPORT = Path(__file__).resolve().parent / "ragas_baseline.jsonl"
CACHE = Path(__file__).resolve().parent / "ragas_retrieval_cache.json"
BASELINES = Path(__file__).resolve().parent / "ragas_baselines.json"


def load_questions() -> list[dict]:
    """优先本地题库（含参考答案），否则公开版。"""
    local = Path(__file__).resolve().parent / "eval_questions.local.json"
    path = local if local.exists() else Path(__file__).resolve().parent / "eval_questions.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return [q for q in data if q["category"] == "kb"]


class ProjectEmbeddingsAdapter(Embeddings):
    """把项目本地 BGE 嵌入包装成 ragas Embeddings（四个指标本身不需要嵌入，
    但 evaluate() 会实例化默认嵌入器——DeepSeek 无嵌入端点且同步客户端
    无法 aembed_text，用本地模型兜底，离线可用）。"""

    def __init__(self, embeddings) -> None:
        self._emb = embeddings

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._emb.embed_documents(list(texts))

    def embed_query(self, text: str) -> list[float]:
        return self._emb.embed_query(text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents(texts)

    async def aembed_query(self, text: str) -> list[float]:
        return self.embed_query(text)

    # collections（下一代 API，--explain 诊断用）接口名不同：text/texts
    def embed_text(self, text: str, **kwargs) -> list[float]:
        return self.embed_query(text)

    async def aembed_text(self, text: str, **kwargs) -> list[float]:
        return self.embed_query(text)

    def embed_texts(self, texts: list[str], **kwargs) -> list[list[float]]:
        return self.embed_documents(list(texts))

    async def aembed_texts(self, texts: list[str], **kwargs) -> list[list[float]]:
        return self.embed_documents(list(texts))


def load_embeddings():
    """加载本地 BGE 嵌入（用于 ragas 的 embeddings 参数）。"""
    from app.config import get_settings
    from app.rag.embeddings import LocalBGEEmbeddings

    settings = get_settings()
    return ProjectEmbeddingsAdapter(
        LocalBGEEmbeddings(settings.embedding_model_dir)
    )


def load_judge(use_async: bool = False):
    """裁判模型：跟随当前激活的对话供应商（与主对话同一账户/余额）。
    注意:runtime_config 的覆盖值(设置页保存的供应商/key)只在后端进程
    启动时从 MySQL 加载;评测脚本是独立进程,必须自己先 load_overrides,
    否则会回退到环境变量里的旧 key(常见坑:设置页换了新 key,评测仍打
    旧账户报 402 Insufficient Balance)。"""
    from app.config import get_settings
    from app.runtime_config import chat_provider_config, load_overrides

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
    api_key = (cfg.get("api_key") or "").strip()
    base_url = (cfg.get("base_url") or "https://api.deepseek.com").strip()
    model = (cfg.get("model") or "deepseek-v4-flash").strip()
    if not api_key:
        api_key = os.environ.get("DEEPSEEK_API_KEY") or ""
    if not api_key:
        raise RuntimeError("未配置对话模型 API Key，无法启动 judge")
    # 裁判固定走 OpenAI 兼容端点（ragas llm_factory 需要 OpenAI 客户端）：
    # 激活供应商若是 Anthropic 兼容格式（base_url 含 /anthropic），剥回根端点；
    # 模型名的 [1m] 长窗口后缀仅 Anthropic 格式接受，这里去掉。
    if "/anthropic" in base_url:
        base_url = re.sub(r"/(?:api/|apps/)?anthropic/?$", "", base_url.rstrip("/"))
    model = re.sub(r"\[1m\]", "", model)
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        # 直连 api.deepseek.com（trust_env=False 绕开系统代理）：httpx 默认
        # trust_env 会走 Clash(127.0.0.1:7890)，代理挂起时请求卡死在
        # FIN_WAIT_2 且无输出（实测全量评测卡 0/84 十四分钟）
        http_client=httpx.Client(trust_env=False),
        # 显式短超时（openai SDK 默认 600s 会覆盖 httpx 客户端级超时，
        # 单条 TCP 流量挂起时会整体卡住；120s 失败后由重试接管）
        timeout=httpx.Timeout(120.0, connect=15.0),
        max_retries=2,
    )
    if use_async:
        # collections（下一代 API）是 async-first：其 score()/ascore() 内部走
        # agenerate，同步客户端会被拒绝（TypeError: Cannot use agenerate()
        # with a synchronous client），诊断模式需要 AsyncOpenAI
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=httpx.AsyncClient(trust_env=False),
            timeout=httpx.Timeout(120.0, connect=15.0),
            max_retries=2,
        )
    return llm_factory(
        model,
        client=client,
        max_tokens=8192,
        temperature=0,
        # 裁判关闭思考：v4 系列默认思考，思考 token 会吃满 max_tokens 导致
        # 输出被截断（IncompleteOutputException → 指标写 None，实测 7/8 题失效）
        extra_body={"thinking": {"type": "disabled"}},
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


def load_cache() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def retrieval_fingerprint() -> str:
    """检索管道的指纹：参数 + 索引物理名 + 嵌入/重排 provider。

    缓存条目带指纹，管道一变（换参数/重建索引/换 provider）该条目自动失效
    重跑检索，避免"改了检索却命中旧缓存"的静默失真。读不到 OpenSearch 时
    退化为只含参数的指纹（仍能挡住参数变更）。"""
    from app.config import get_settings
    from app.rag.retriever import RETRIEVAL_VERSION
    from app.runtime_config import effective

    s = get_settings()
    physical = ""
    try:
        r = httpx.get(
            f"{s.opensearch_url.rstrip('/')}/_alias/{s.opensearch_index}",
            timeout=5,
            trust_env=False,
        )
        if r.status_code == 200:
            physical = next(iter(r.json().keys()), "")
    except Exception:
        pass
    parts = [
        RETRIEVAL_VERSION,
        s.recall_k,
        s.candidate_pool,
        s.rerank_top_k,
        s.max_parents,
        int(bool(s.query_expansion_enabled)),
        int(bool(s.parent_child_enabled)),
        str(effective(s, "embedding_provider") or "local"),
        str(effective(s, "reranker_provider") or "local"),
        physical,
    ]
    return ":".join(str(p) for p in parts)


def fetch_retrieval(q: dict, cache: dict, refresh: bool, fp: str) -> tuple[dict | None, str]:
    """返回 (data, source)。source: live=本次调用 / cache=复用 / fail=失败。"""
    entry = cache.get(q["id"])
    if (
        not refresh
        and entry
        and entry.get("question") == q["question"]
        and entry.get("fp") == fp
    ):
        return {"answer": entry.get("answer", ""), "sources": entry.get("sources") or []}, "cache"
    data = None
    for attempt in range(2):
        try:
            data = rag_chat(q["question"])
            break
        except Exception as exc:
            print(f"[{q['id']}] 重试 {attempt + 1}：{str(exc)[:80]}", flush=True)
            time.sleep(6)
    if data is None:
        return None, "fail"
    # 逐题落盘：即使 judge 阶段卡死/中断，检索结果也不白跑
    cache[q["id"]] = {
        "question": q["question"],
        "answer": data.get("answer", ""),
        "sources": data.get("sources") or [],
        "fp": fp,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_cache(cache)
    return data, "live"


def run_explain(ids_str: str) -> None:
    """诊断模式：用 ragas.metrics.collections（下一代 API）对指定题单跑，打印
    逐条判定理由（MetricResult.value + reason）——回答"裁判为什么打低分"。
    复用检索缓存、不写报告；collections 是 ragas 1.0 的标准形态，这里刻意
    只作诊断，不替换正式评测口径（正式仍用 legacy+evaluate()，理由见
    docs/RAGAS_EVAL_GUIDE.md"为什么不用新 API"）。"""
    kb = {q["id"]: q for q in load_questions()}
    cache = load_cache()
    judge = load_judge(use_async=True)
    embeddings = load_embeddings()
    from ragas.metrics.collections import (
        AnswerCorrectness as CAnswerCorrectness,
        ContextRecall as CContextRecall,
        Faithfulness as CFaithfulness,
    )

    for qid in [i.strip() for i in ids_str.split(",") if i.strip()]:
        q = kb.get(qid)
        entry = cache.get(qid)
        if not q or not entry:
            print(f"[{qid}] 题库或检索缓存缺失，跳过", flush=True)
            continue
        contexts = [s.get("content", "") for s in (entry.get("sources") or [])]
        response = entry.get("answer", "")
        reference = (q.get("reference") or "").strip()
        print("=" * 88)
        print(f"[{qid}] {q['question']}", flush=True)
        calls = [
            (
                "Faithfulness",
                CFaithfulness(llm=judge),
                dict(
                    user_input=q["question"],
                    response=response,
                    retrieved_contexts=contexts,
                ),
            ),
        ]
        if reference:
            calls += [
                (
                    "ContextRecall",
                    CContextRecall(llm=judge),
                    dict(
                        user_input=q["question"],
                        retrieved_contexts=contexts,
                        reference=reference,
                    ),
                ),
                (
                    "AnswerCorrectness",
                    # collections 版默认权重 [0.75,0.25]，语义那半需要嵌入（本地 BGE）
                    CAnswerCorrectness(llm=judge, embeddings=embeddings),
                    dict(
                        user_input=q["question"],
                        response=response,
                        reference=reference,
                    ),
                ),
            ]
        for name, metric, kwargs in calls:
            try:
                res = metric.score(**kwargs)
                value = getattr(res, "value", res)
                reason = getattr(res, "reason", None)
                print(f"  {name}: {value}", flush=True)
                if reason:
                    for line in str(reason).splitlines():
                        print(f"    | {line}", flush=True)
            except Exception as exc:
                print(
                    f"  {name}: 诊断失败 {type(exc).__name__}: {str(exc)[:120]}",
                    flush=True,
                )


def write_baseline(tag: str, rows_: list[dict]) -> Path:
    """把本次 run 的均值与逐题值写入基线 rails（regression_gate --baseline 读取）。"""
    data: dict = {}
    if BASELINES.exists():
        try:
            data = json.loads(BASELINES.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    keys = ["faithfulness", "answer_correctness", "context_precision", "context_recall"]
    entry: dict = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ids": sorted(r["id"] for r in rows_),
        "per_question": {
            r["id"]: {k: (r.get(k) if isinstance(r.get(k), (int, float)) else None) for k in keys}
            for r in rows_
        },
    }
    for k in keys:
        entry[k] = _avg(rows_, k)
    data[tag] = entry
    BASELINES.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return BASELINES


def append_report(row: dict) -> None:
    with REPORT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def resume_state() -> tuple[str | None, dict[str, dict]]:
    """读取 REPORT 中最后一次 run 的 (run_id, 已完成 id->row)。"""
    if not REPORT.exists():
        return None, {}
    rows = []
    for line in REPORT.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    if not rows:
        return None, {}
    run_id = rows[-1].get("run_id")
    if not run_id:
        return None, {}
    done = {}
    for r in rows:
        if r.get("run_id") == run_id and isinstance(r.get("faithfulness"), (int, float)):
            done[r["id"]] = r
    return run_id, done


def _avg(rows_: list[dict], key: str):
    vals = [r[key] for r in rows_ if isinstance(r.get(key), (int, float))]
    return round(sum(vals) / len(vals), 3) if vals else None


def _print_summary(rows_: list[dict], run_id: str) -> None:
    if not rows_:
        return
    keys = ["faithfulness", "answer_correctness", "context_precision", "context_recall"]
    labels = ["faith", "ac", "prec", "rec"]
    print(f"\n本次结果（run_id={run_id}，{len(rows_)} 题）：")
    print(f"  {'id':<11}" + "".join(f"{lab:>9}" for lab in labels) + f"{'ctx':>6}")
    for r in rows_:
        cells = []
        for k in keys:
            v = r.get(k)
            cells.append("—".rjust(9) if not isinstance(v, (int, float)) else format(v, ".3f").rjust(9))
        print(f"  {r['id']:<11}" + "".join(cells) + f"{str(r.get('n_contexts', '')):>6}")
    print(f"均值（{len(rows_)} 题）: " + " ".join(f"{k}={_avg(rows_, k)}" for k in keys))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--id", default="", help="按 id 过滤，多个用逗号分隔")
    parser.add_argument("--refresh", action="store_true", help="忽略检索缓存，重新调用 /api/chat")
    parser.add_argument("--resume", action="store_true", help="续跑上一次中断的 run（跳过已完成题）")
    parser.add_argument("--tag", default="", help="本次结果确认为新基线时写入 ragas_baselines.json 的名字")
    parser.add_argument("--explain", default="", help="诊断模式：对指定题（逗号分隔）打印裁判逐条判定理由，不写报告")
    args = parser.parse_args()
    if args.explain:
        run_explain(args.explain)
        return
    kb_all = load_questions()
    if args.id:
        wanted = {i.strip() for i in args.id.split(",") if i.strip()}
        kb_all = [q for q in kb_all if q["id"] in wanted]
    if args.limit:
        kb_all = kb_all[: args.limit]
    if not kb_all:
        print("没有匹配的题目")
        return

    run_id = time.strftime("%Y-%m-%dT%H:%M:%S")
    resumed: dict[str, dict] = {}
    kb = kb_all
    if args.resume:
        last_run, done = resume_state()
        if last_run is None:
            print("--resume 未找到可续跑的 run（历史行缺少 run_id），按全新 run 执行")
        else:
            # 题干被改过的题不算已完成（旧结论已失效）
            qtext = {q["id"]: q["question"] for q in kb_all}
            done = {qid: r for qid, r in done.items() if qtext.get(qid) == r.get("question")}
            kb = [q for q in kb_all if q["id"] not in done]
            if not kb:
                print(f"run {last_run} 已完成全部 {len(done)} 题，无需续跑")
                _print_summary([done[q["id"]] for q in kb_all if q["id"] in done], last_run)
                return
            resumed = {q["id"]: done[q["id"]] for q in kb_all if q["id"] in done}
            run_id = last_run
            print(f"续跑 run {last_run}：已完成 {len(resumed)} 题（跳过），本次剩 {len(kb)} 题")

    judge = load_judge()
    embeddings = load_embeddings()
    cache = load_cache()
    fp = retrieval_fingerprint()
    n_valid = n_stale = 0
    for q in kb:
        e = cache.get(q["id"])
        if not e or e.get("question") != q["question"]:
            continue
        if e.get("fp") == fp:
            n_valid += 1
        else:
            n_stale += 1
    print(
        f"run_id={run_id} | 本次评测 {len(kb)} 题 | 检索缓存：有效 {n_valid}，"
        f"管道指纹失效 {n_stale}，无缓存 {len(kb) - n_valid - n_stale}"
        + ("（--refresh：全部重跑检索）" if args.refresh else "")
    )
    print("judge 阶段若卡死：中断后重跑，检索默认复用缓存；评测加 --resume 续跑\n")

    samples = []
    rows = []
    for q in kb:
        data, source = fetch_retrieval(q, cache, args.refresh, fp)
        if data is None:
            print(f"[{q['id']}] 检索失败（已重试 2 次），跳过；可单独重跑：--id {q['id']}", flush=True)
            continue
        contexts = [s.get("content", "") for s in (data.get("sources") or [])]
        sample_kwargs = dict(
            user_input=q["question"],
            response=data.get("answer", ""),
            retrieved_contexts=contexts,
        )
        reference = (q.get("reference") or "").strip()
        if reference:
            sample_kwargs["reference"] = reference
        samples.append(SingleTurnSample(**sample_kwargs))
        rows.append(
            {
                "id": q["id"],
                "question": q["question"],
                "run_id": run_id,
                "retrieval": source,
                "n_contexts": len(contexts),
                "has_reference": bool(reference),
                "skip_reference_metrics": bool(q.get("skip_reference_metrics")),
                "skip_faithfulness": bool(q.get("skip_faithfulness")),
                "answer_head": (data.get("answer") or "")[:120],
            }
        )
        print(f"[{q['id']}] 检索{'命中缓存' if source == 'cache' else '完成'}，来源 {len(contexts)} 条", flush=True)
    if not rows:
        print("没有可评测的样本（检索全部失败）")
        return

    # ragas to_pandas 的列名是 snake_case（faithfulness / answer_correctness ...）
    COLUMN_KEYS = {
        "Faithfulness": "faithfulness",
        "AnswerCorrectness": "answer_correctness",
        "ContextPrecision": "context_precision",
        "ContextRecall": "context_recall",
    }
    SHORT = {"Faithfulness": "faith", "AnswerCorrectness": "ac", "ContextPrecision": "prec", "ContextRecall": "rec"}
    faith_m = Faithfulness(llm=judge)
    # 事实 F1 × 0.5 + 语义相似度 × 0.5（2026-09-11 实验结论：纯事实口径
    # weights=[1,0] 对"答对但措辞不同"系统性扣分，6 题对照均值 +0.30；
    # 语义半边用本地 BGE 适配器，见 docs/RAGAS_EVAL_GUIDE.md）
    ac_m = AnswerCorrectness(llm=judge, embeddings=embeddings, weights=[0.5, 0.5])
    cp_m = ContextPrecision(llm=judge)
    cr_m = ContextRecall(llm=judge)
    run_cfg = RunConfig(max_workers=4, timeout=300, max_retries=3)
    n_ref = sum(r["has_reference"] for r in rows)
    n_skip = sum(1 for r in rows if r.get("skip_reference_metrics"))
    n_skip_faith = sum(1 for r in rows if r.get("skip_faithfulness"))
    print(
        f"指标：Faithfulness +（带参考 {n_ref}/{len(rows)} 题）AnswerCorrectness/ContextPrecision/ContextRecall"
        + (f"，其中 {n_skip} 题声明 skip_reference_metrics 仅测 faithfulness" if n_skip else "")
        + (f"，{n_skip_faith} 题声明 skip_faithfulness 全部停测" if n_skip_faith else ""),
        flush=True,
    )

    # 逐题评测并即时落盘：某题卡死/中断只损失该题，已完成的题 --resume 不重复
    new_rows = []
    for i, (q, sample, row) in enumerate(zip(kb, samples, rows), 1):
        # skip_reference_metrics：拒答类题的参考答案描述“库里没有”，与
        # AnswerCorrectness/Context* 的语义错位，只测 faithfulness；
        # skip_faithfulness：加 S5 门槛后拒答题返回空上下文，faithfulness
        # 对空上下文恒为 0，无意义，直接全部停测。
        skip_ref = bool(q.get("skip_reference_metrics"))
        skip_faith = bool(q.get("skip_faithfulness"))
        metrics = []
        if not skip_faith:
            metrics.append(faith_m)
        if row["has_reference"] and not skip_ref:
            metrics += [ac_m, cp_m, cr_m]
        mrow = None
        if metrics:
            try:
                res = evaluate(
                    EvaluationDataset(samples=[sample]),
                    metrics=metrics,
                    llm=judge,
                    embeddings=embeddings,
                    # 并发降到 4（默认 16 容易触发 DeepSeek 限流）；超时/重试收紧，
                    # 单次调用最多等 5 分钟、重试 3 次，卡住也能快速失败而不是整体挂起
                    run_config=run_cfg,
                )
                mrow = res.to_pandas().iloc[0]
            except Exception as exc:
                print(f"[{q['id']}] 评测异常：{type(exc).__name__}: {str(exc)[:100]}（指标记 None，--resume 会重试该题）", flush=True)
        else:
            print(f"[{q['id']}] 声明跳过全部指标（skip_faithfulness），仅验证行为", flush=True)
        for key in COLUMN_KEYS.values():
            row[key] = None
        for m in metrics:
            key = COLUMN_KEYS[type(m).__name__]
            value = mrow.get(key) if mrow is not None else None
            # pandas 缺失值是 float('nan') 而非 None,统一归一为 None
            row[key] = None if value is None or (isinstance(value, float) and value != value) else float(value)
        append_report(row)
        new_rows.append(row)
        parts = []
        for m in metrics:
            key = COLUMN_KEYS[type(m).__name__]
            v = row[key]
            parts.append(f"{SHORT[type(m).__name__]}={'—' if v is None else format(v, '.3f')}")
        print(f"[{i}/{len(rows)}] {q['id']} " + " ".join(parts), flush=True)

    done_map = {**resumed, **{r["id"]: r for r in new_rows}}
    summary_rows = [done_map[q["id"]] for q in kb_all if q["id"] in done_map]
    _print_summary(summary_rows, run_id)
    print("结果已写入：", REPORT, "；检索缓存在：", CACHE)
    if args.tag:
        path = write_baseline(args.tag, summary_rows)
        print(
            f"基线已写入 {path}：tag={args.tag}，{len(summary_rows)} 题 "
            f"faith={_avg(summary_rows, 'faithfulness')} ac={_avg(summary_rows, 'answer_correctness')} "
            f"prec={_avg(summary_rows, 'context_precision')} rec={_avg(summary_rows, 'context_recall')}；"
            f"回归用：python scripts/regression_gate.py --skip-eval --baseline {args.tag}"
        )


if __name__ == "__main__":
    main()
