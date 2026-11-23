"""ApiTokenMiddleware 单元测试：401 / 令牌通过 / 探针放行 / 预检放行。

不依赖 MySQL / OpenSearch：用最小 FastAPI 应用直接挂中间件验证行为；
Settings.api_token 的环境变量读取单独验证。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api_auth import ApiTokenMiddleware


def _build(token: str = "s3cret") -> TestClient:
    app = FastAPI()

    @app.get("/api/hello")
    def hello() -> dict:
        return {"ok": True}

    @app.get("/api/livez")
    def livez() -> dict:
        return {"status": "ok"}

    @app.get("/other")
    def other() -> dict:
        return {"ok": True}

    app.add_middleware(
        ApiTokenMiddleware, token=token, exempt_paths=frozenset({"/api/livez"})
    )
    return TestClient(app)


def test_missing_token_401():
    client = _build()
    resp = client.get("/api/hello")
    assert resp.status_code == 401
    assert "API Token" in resp.json()["detail"]
    # 前端 axios 拦截器靠这个头区分"全局 token 401"与提示词管理 401
    assert resp.headers.get("x-zhiwen-unauthorized") == "api-token"


def test_wrong_token_401():
    client = _build()
    resp = client.get("/api/hello", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_bearer_token_ok():
    client = _build()
    resp = client.get("/api/hello", headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_bearer_prefix_case_insensitive():
    client = _build()
    resp = client.get("/api/hello", headers={"Authorization": "bearer s3cret"})
    assert resp.status_code == 200


def test_custom_header_ok():
    client = _build()
    resp = client.get("/api/hello", headers={"X-Zhiwen-Token": "s3cret"})
    assert resp.status_code == 200


def test_probe_exempt():
    client = _build()
    assert client.get("/api/livez").status_code == 200


def test_non_api_path_not_guarded():
    client = _build()
    assert client.get("/other").status_code == 200


def test_options_preflight_not_blocked():
    """预检不带 Authorization：中间件放行（实际请求仍会校验）。"""
    client = _build()
    resp = client.options("/api/hello")
    # 路由本身没有 OPTIONS → FastAPI 返回 405；只要不是 401 就证明未被拦截
    assert resp.status_code != 401


def test_settings_api_token_from_env(monkeypatch):
    monkeypatch.setenv("ZHIWEN_API_TOKEN", " abc ")
    from app.config import Settings

    assert Settings.from_env().api_token == "abc"

    monkeypatch.delenv("ZHIWEN_API_TOKEN")
    assert Settings.from_env().api_token == ""
