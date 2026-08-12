"""并发测试：3 个问答请求同时发起，验证并发限制与监控计数。"""

from __future__ import annotations

import concurrent.futures
import time

import httpx

BASE = "http://127.0.0.1:8000"


def ask(index: int) -> tuple[int, str]:
    question = (
        "什么是“实事求是”？"
        if index % 2 == 0
        else "《示例书》中主要矛盾和次要矛盾的关系是什么？"
    )
    response = httpx.post(
        f"{BASE}/api/chat",
        json={"question": question, "history": []},
        timeout=600,
    )
    answer = response.json().get("answer", response.text)[:40]
    return response.status_code, answer


def main() -> None:
    start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(ask, range(3)))
    for result in results:
        print(result)
    print(f"3 个并发请求总耗时：{time.time() - start:.1f}s")
    metrics = httpx.get(f"{BASE}/api/metrics", timeout=30).json()
    print("metrics:", metrics)


if __name__ == "__main__":
    main()
