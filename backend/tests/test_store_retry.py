"""OpenSearch 客户端瞬时重试单测。

2026-09-18：本机出现数秒级的 "all shards failed"（503）窗口，用户检索
被这类瞬时故障打断——查询类路径加短退避重试消化（幂等读，安全）。
"""

import httpx
import pytest

from app.rag.store import OpenSearchStore


def _resp(method: str, path: str, status: int = 200, json_body=None):
    request = httpx.Request(method, "http://x" + path)
    if json_body is None:
        return httpx.Response(status, request=request)
    return httpx.Response(status, json=json_body, request=request)


class _FlakyClient:
    """前 fail_times 次按 mode 失败（503 / 连接错误），之后成功。"""

    def __init__(self, fail_times: int, mode: str = "503"):
        self.fail_times = fail_times
        self.mode = mode
        self.calls = 0

    def request(self, method, path, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            if self.mode == "503":
                return _resp(method, path, status=503)
            raise httpx.ConnectError(
                "refused", request=httpx.Request(method, "http://x" + path)
            )
        return _resp(method, path, json_body={"ok": True})


def _store_with(client) -> OpenSearchStore:
    store = OpenSearchStore()
    store.client = client
    return store


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("app.rag.store.time.sleep", lambda *_a: None)


def test_search_retries_on_503_then_succeeds():
    client = _FlakyClient(fail_times=2)
    store = _store_with(client)
    assert store._request("POST", "/idx/_search", json={}) == {"ok": True}
    assert client.calls == 3


def test_search_retries_on_connect_error():
    client = _FlakyClient(fail_times=1, mode="connect")
    store = _store_with(client)
    assert store._request("POST", "/idx/_search", json={}) == {"ok": True}
    assert client.calls == 2


def test_count_and_delete_by_query_also_retry():
    client = _FlakyClient(fail_times=1)
    store = _store_with(client)
    assert store._request("POST", "/idx/_count", json={}) == {"ok": True}
    assert client.calls == 2

    client2 = _FlakyClient(fail_times=1)
    store2 = _store_with(client2)
    assert store2._request("POST", "/idx/_delete_by_query", json={}) == {"ok": True}
    assert client2.calls == 2


def test_search_gives_up_after_retries():
    client = _FlakyClient(fail_times=99)
    store = _store_with(client)
    with pytest.raises(httpx.HTTPStatusError):
        store._request("POST", "/idx/_search", json={})
    assert client.calls == 3


def test_non_query_path_no_retry():
    client = _FlakyClient(fail_times=99)
    store = _store_with(client)
    with pytest.raises(httpx.HTTPStatusError):
        store._request("GET", "/idx/_mapping")
    assert client.calls == 1
