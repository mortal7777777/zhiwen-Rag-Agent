"""技能选取评测：L0 索引可见性 + 检索 top-k 命中率。

用法（在 backend/ 下执行）：
    python scripts/dev/eval_skills.py --mode index            # 无 GPU，检查索引
    python scripts/dev/eval_skills.py --mode view             # 真实 BGE 嵌入
    python scripts/dev/eval_skills.py --mode view --rerank    # 叠加 bge-reranker

说明：
- index 模式检查"期望技能名称是否出现在 L0 索引里"（主路径已改为模型按名加载，
  索引可见性是第一指标）；view 模式保留旧检索链，便于与 2026-10-21 基线对比。
- 默认对全部扫描到的技能评测；可传 --enabled a,b,c 限定集合。
- 无对应技能的探针（新建目录/浏览器预览）单独统计，不计入 top-k 准确率。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# (query, 期望技能名列表；空列表 = 无匹配探针)
GOLDEN: list[tuple[str, list[str]]] = [
    ("帮我写一个 Word 文档", ["docx"]),
    ("用 React 写一个漂亮的落地页", ["frontend-design", "design-taste-frontend"]),
    ("先写测试再实现", ["test-driven-development"]),
    ("画一张系统架构图", ["architecture-diagram"]),
    ("帮我把这段话润色", ["humanizer"]),
    ("处理这个 PDF 文件并提取表格", ["pdf"]),
    ("分析这个 CSV 文件", ["xlsx"]),
    ("调试这个 Python 报错", ["systematic-debugging", "python-debugpy"]),
    ("帮我做一次代码审查", ["requesting-code-review", "github-code-review"]),
    ("制定一个项目开发计划", ["create-plan", "plan"]),
    ("搜索一下 arXiv 上的论文", ["arxiv"]),
    ("做一张信息图", ["baoyu-infographic", "canvas-design"]),
    ("从 YouTube 视频里提取内容", ["youtube-content"]),
    ("把下载目录里的文件整理归类", ["file-organizer"]),
    ("针对这个岗位定制我的简历", ["tailored-resume-generator"]),
    ("用 TDD 重构这段代码", ["test-driven-development", "simplify-code"]),
    ("新建一个空目录", []),
    ("在浏览器里打开预览这个页面", []),
    ("单文件 SVG 动画 HTML 页面 art deco 多层景深", ["algorithmic-art", "canvas-design"]),
    ("把这份会议记录整理成待办事项", ["meeting-notes-and-actions"]),
]


def _load_skills(enabled: set[str] | None) -> list[dict]:
    from app.skills import scan_skills

    skills = scan_skills()
    if enabled is not None:
        skills = [s for s in skills if s["name"] in enabled]
    return skills


def run_index(skills: list[dict], budget: int) -> int:
    from app.skills import build_skill_index

    index = build_skill_index(skills, budget=budget)
    visible = {s["name"] for s in skills if s.get("platform_ok", True)}
    missing = 0
    for query, expected in GOLDEN:
        if not expected:
            continue
        absent = [name for name in expected if name not in visible]
        if absent:
            missing += 1
            print(f"MISS {query} -> 期望技能未扫描到：{absent}")
    print(
        f"index: visible={index.visible}/{len(skills)} "
        f"names_only={index.names_only} chars={index.chars} "
        f"missing_expected={missing} signature={index.signature[:16]}…"
    )
    return 0 if missing == 0 else 1


def _rerank(service, query: str, skills: list[dict], top_k: int) -> list[str]:
    from langchain_core.documents import Document

    docs = [
        Document(
            page_content=(
                f"{s['name']} {s.get('description', '')} {s.get('excerpt', '')[:800]}"
            ),
            metadata={"name": s["name"]},
        )
        for s in skills
    ]
    ranked = service.reranker.rerank(query, docs, top_k=top_k)
    return [d.metadata["name"] for d in ranked]


def run_view(skills: list[dict], rerank: bool) -> int:
    from app.api.deps import get_service
    from app.skills import search_skills

    service = get_service()
    top1 = top3 = total = 0
    no_match_hits = 0
    for query, expected in GOLDEN:
        if not expected:
            hits = search_skills(
                query, embeddings=service.embeddings, top_k=20, enabled_ids=None
            )
            name = hits[0]["name"] if hits else "-"
            if name != "-":
                no_match_hits += 1
            print(f"NO-MATCH {query} -> 检索仍返回 {name}")
            continue
        total += 1
        if rerank:
            names = _rerank(service, query, list(skills), top_k=3)
        else:
            hits = search_skills(
                query, embeddings=service.embeddings, top_k=3, enabled_ids=None
            )
            names = [h["name"] for h in hits]
        if names and names[0] in expected:
            top1 += 1
        if any(name in expected for name in names):
            top3 += 1
        else:
            print(f"MISS {query} -> {names}")
    print(
        f"selection: top1={top1}/{total} top3={top3}/{total} "
        f"no_match_spurious={no_match_hits}/{sum(1 for _, e in GOLDEN if not e)}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("index", "view"), default="index")
    parser.add_argument("--rerank", action="store_true", help="view 模式叠加重排")
    parser.add_argument("--budget", type=int, default=12000, help="index 模式字符预算")
    parser.add_argument("--enabled", default="", help="逗号分隔的启用集合（默认全部）")
    args = parser.parse_args()

    enabled = {x.strip() for x in args.enabled.split(",") if x.strip()} or None
    skills = _load_skills(enabled)
    print(f"技能总数：{len(skills)}")
    if args.mode == "index":
        return run_index(skills, args.budget)
    return run_view(skills, args.rerank)


if __name__ == "__main__":
    raise SystemExit(main())
