"""端到端问答演示：调用正在运行的后端（默认 http://127.0.0.1:8000）。

运行：
    python chat_demo.py
"""

from __future__ import annotations

import httpx

BASE = "http://127.0.0.1:8000"

QUESTIONS = [
    "某书如何分析战争的阶段划分？",
    "什么是“实事求是”？",
    "某书中主次矛盾的关系是什么？",
    "群众路线的基本内容是什么？",
    "“枪杆子里面出政权”是什么意思？",
]


def main() -> None:
    client = httpx.Client(base_url=BASE, timeout=600)
    for question in QUESTIONS:
        response = client.post("/api/chat", json={"question": question, "history": []})
        print("=" * 70)
        print("Q:", question, "| status:", response.status_code)
        if response.status_code == 200:
            data = response.json()
            print("A:", data["answer"])
            for i, source in enumerate(data["sources"], 1):
                text = source["content"].replace("\n", " ")
                print(f"  [{i}] score={source['score']} | {text[:60]}")
        else:
            print("detail:", response.text)


if __name__ == "__main__":
    main()
