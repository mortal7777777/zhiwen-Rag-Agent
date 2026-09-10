"""v2 管线端到端问答演示（Parent-Child + Query 改写 + 精排 + 生成）。

运行（在 backend 目录下，需要 DEEPSEEK_API_KEY）：
    python ask_demo_v2.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("OPENSEARCH_INDEX", "zhiwen_kb_current")

from app.config import get_settings  # noqa: E402
from app.rag.service import RAGService  # noqa: E402

QUESTIONS = [
    "某本书的主要观点是什么？",
    "什么是“实事求是”？",
    "某书中主次矛盾的关系是什么？",
]


def main() -> None:
    service = RAGService(get_settings())
    for question in QUESTIONS:
        print("=" * 70)
        result = service.ask(question, history=[])
        print("Q:", question)
        print("A:", result["answer"])
        for i, source in enumerate(result["sources"], 1):
            text = source["content"].replace("\n", " ")[:60]
            page = source.get("page")
            print(f"  [{i}] score={source['score']} page={page} | {text}")


if __name__ == "__main__":
    main()
