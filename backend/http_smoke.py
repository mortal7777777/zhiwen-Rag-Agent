"""后端冒烟测试：健康检查、普通问答、SSE 流式问答、监控指标。"""

from __future__ import annotations

import httpx

BASE = "http://127.0.0.1:8000"


def main() -> None:
    client = httpx.Client(base_url=BASE, timeout=600)

    print("health:", client.get("/api/health").json())
    print("metrics(初始):", client.get("/api/metrics").json())

    r = client.post("/api/chat", json={"question": "什么是“实事求是”？", "history": []})
    print("普通问答:", r.status_code, r.json()["answer"][:60])

    print("\nSSE 流式问答：")
    tokens = 0
    with client.stream(
        "POST",
        "/api/chat/stream",
        json={"question": "《示例书》的主要观点是什么？", "history": []},
    ) as response:
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            import json

            payload = json.loads(line[6:])
            event = payload["event"]
            if event == "sources":
                print(f"  [sources] {len(payload['data'])} 条，page={[s.get('page') for s in payload['data']]}")
            elif event == "token":
                tokens += 1
            elif event == "done":
                print("  [done]")
            elif event == "error":
                print("  [error]", payload["data"])
    print(f"  共收到 {tokens} 个 token 事件")

    print("\nmetrics(运行后):", client.get("/api/metrics").json())


if __name__ == "__main__":
    main()
