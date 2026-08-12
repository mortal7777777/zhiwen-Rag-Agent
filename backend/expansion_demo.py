"""查询扩展演示：Multi-Query / HyDE / 多轮补全。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("OPENSEARCH_INDEX", "rag_knowledge_base_v2")

from app.config import get_settings  # noqa: E402
from app.rag.service import RAGService  # noqa: E402


def main() -> None:
    service = RAGService(get_settings())

    print("=== 1. 模糊问题扩展（Multi-Query + HyDE）===")
    question = "为什么说抗日战争一定会胜利？"
    queries = service.query_expander.expand(question)
    for i, query in enumerate(queries, 1):
        print(f"  {i}. {query[:80]}")

    print("\n=== 2. 多轮补全（上下文消歧）===")
    history = [
        {"role": "user", "content": "《示例书》讲了什么？"},
        {"role": "assistant", "content": "它讲了认识和实践的关系。"},
    ]
    follow_up = "它的主要观点是什么？"
    standalone = service.chat.disambiguate(follow_up, history)
    print(f"  追问：{follow_up}")
    print(f"  补全：{standalone}")

    print("\n=== 3. 多轮场景检索对比（扩展前后）===")
    for label, rewrite in (("不扩展", False), ("扩展", True)):
        docs = service.retrieve(
            follow_up,
            history=history,
            rewrite=rewrite,
            parent_child=True,
        )
        hits = sum(1 for doc in docs if "示例书" in doc.page_content)
        pages = [doc.metadata.get("page") for doc in docs]
        print(f"  [{label}] 命中「示例书」{hits}/4，pages={pages}")


if __name__ == "__main__":
    main()
