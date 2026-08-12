"""HTTP 问答稳定性测试：同一问题连续问多次，检查回答与来源是否稳定。"""

from __future__ import annotations

import httpx

BASE = "http://127.0.0.1:8000"


def main() -> None:
    client = httpx.Client(base_url=BASE, timeout=600)
    question = "什么是“实事求是”？"
    for i in range(1, 4):
        response = client.post("/api/chat", json={"question": question, "history": []})
        data = response.json()
        print(f"=== 第 {i} 次 ===")
        print("A:", data["answer"][:80])
        for source in data["sources"]:
            text = source["content"].replace("\n", " ")[:36]
            print(f"  {source['score']} page={source.get('page')} | {text}")


if __name__ == "__main__":
    main()
