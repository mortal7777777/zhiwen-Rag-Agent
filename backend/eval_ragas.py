"""RAGAS 基线评测：检索 + 生成双维度指标。

用法（backend 目录）：
    python eval_ragas.py --limit 3

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
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from openai import OpenAI
from ragas import EvaluationDataset, SingleTurnSample, evaluate
from ragas.embeddings.base import Embeddings
from ragas.llms import llm_factory
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


def load_embeddings():
    """加载本地 BGE 嵌入（用于 ragas 的 embeddings 参数）。"""
    from app.config import get_settings
    from app.rag.embeddings import LocalBGEEmbeddings

    settings = get_settings()
    return ProjectEmbeddingsAdapter(
        LocalBGEEmbeddings(settings.embedding_model_dir)
    )


def load_judge():
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
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--id", default="")
    args = parser.parse_args()
    kb = load_questions()
    if args.id:
        kb = [q for q in kb if q["id"] == args.id]
    if args.limit:
        kb = kb[: args.limit]

    judge = load_judge()
    embeddings = load_embeddings()
    samples = []
    rows = []
    for q in kb:
        data = None
        for attempt in range(2):
            try:
                data = rag_chat(q["question"])
                break
            except Exception as exc:
                print(f"[{q['id']}] 重试 {attempt + 1}：{str(exc)[:80]}", flush=True)
                time.sleep(6)
        if data is None:
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
                "n_contexts": len(contexts),
                "has_reference": bool(reference),
                "answer_head": (data.get("answer") or "")[:120],
            }
        )

    # 指标按可用数据组合：faithfulness 必测；带参考答案的题追加三个指标
    metrics = [Faithfulness(llm=judge)]
    if any(r["has_reference"] for r in rows):
        metrics += [
            # weights=[1.0, 0.0] = 纯事实性对比,不依赖语义相似度嵌入
            AnswerCorrectness(llm=judge, weights=[1.0, 0.0]),
            ContextPrecision(llm=judge),
            ContextRecall(llm=judge),
        ]
    # ragas to_pandas 的列名是 snake_case（faithfulness / answer_correctness ...）
    COLUMN_KEYS = {
        "Faithfulness": "faithfulness",
        "AnswerCorrectness": "answer_correctness",
        "ContextPrecision": "context_precision",
        "ContextRecall": "context_recall",
    }
    metric_names = [type(m).__name__ for m in metrics]
    print(f"指标：{', '.join(metric_names)}（参考答案仅 {sum(r['has_reference'] for r in rows)}/{len(rows)} 题有，无参考的题该三项为 None）\n")

    dataset = EvaluationDataset(samples=samples)
    result = evaluate(
        dataset,
        metrics=metrics,
        llm=judge,
        embeddings=embeddings,
    )
    df = result.to_pandas()
    print(df.to_string(index=False))

    for row, (_, mrow) in zip(rows, df.iterrows()):
        for name in metric_names:
            key = COLUMN_KEYS[name]
            value = mrow.get(key)
            # pandas 缺失值是 float('nan') 而非 None,统一归一为 None
            row[key] = None if value is None or (isinstance(value, float) and value != value) else float(value)
    with REPORT.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    # 汇总
    with_reference = [r for r in rows if r["has_reference"]]
    if with_reference:
        def _avg(rows_, key):
            vals = [r[key] for r in rows_ if r.get(key) is not None]
            return round(sum(vals) / len(vals), 3) if vals else None

        print(
            "\n带参考答案汇总（{} 题）: faithfulness={} answer_correctness={} "
            "context_precision={} context_recall={}".format(
                len(with_reference),
                _avg(rows, "faithfulness"),
                _avg(with_reference, "answer_correctness"),
                _avg(with_reference, "context_precision"),
                _avg(with_reference, "context_recall"),
            )
        )
    print("结果已写入：", REPORT)


if __name__ == "__main__":
    main()
