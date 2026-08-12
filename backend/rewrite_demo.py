"""Query 改写效果演示：针对模糊/口语化问题，对比改写前后的检索结果。

运行（在 backend 目录下，需要 DEEPSEEK_API_KEY）：
    python rewrite_demo.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("OPENSEARCH_INDEX", "rag_knowledge_base_v2")

from app.config import get_settings  # noqa: E402
from app.rag.service import RAGService  # noqa: E402

# 模糊/口语化问题 -> 期望命中的核心词
AMBIGUOUS_QUESTIONS = [
    ("那篇讲实践和认识关系的文章，主要说了什么？", "示例书"),
    ("主要矛盾和次要矛盾怎么区分？", "矛盾"),
    ("他说打仗要靠什么才能赢？", "枪杆子"),
    ("为什么说抗日战争一定会胜利？", "持久战"),
]


def preview(text: str, length: int = 40) -> str:
    return text.replace("\n", " ").strip()[:length]


def main() -> None:
    service = RAGService(get_settings())
    for question, keyword in AMBIGUOUS_QUESTIONS:
        print("=" * 70)
        print("问题：", question)

        rewritten = service.chat.rewrite_queries(
            question,
            n=service.settings.rewrite_variants,
        )
        print("改写后的查询：")
        for query in rewritten:
            print("  -", query)

        for label, rewrite in (("不改写", False), ("改写", True)):
            docs = service.retrieve(question, rewrite=rewrite, parent_child=True)
            hits = sum(1 for doc in docs if keyword in doc.page_content)
            top1 = preview(docs[0].page_content, 40) if docs else "-"
            print(f"[{label}] 命中「{keyword}」{hits}/4 | top1: {top1}")


if __name__ == "__main__":
    main()
