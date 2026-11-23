"""健康探针与 request_id 中间件（不依赖真实 MySQL/OpenSearch）。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import health as health_mod
from app.logging_config import RequestContextMiddleware


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(health_mod.router, prefix="/api")
    return TestClient(app)


def test_livez_ok():
    response = _client().get("/api/livez")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_ok(monkeypatch):
    monkeypatch.setattr(
        health_mod,
        "_dependencies",
        lambda: {"mysql": {"ok": True}, "opensearch": {"ok": True}},
    )
    response = _client().get("/api/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_degraded_returns_503(monkeypatch):
    monkeypatch.setattr(
        health_mod,
        "_dependencies",
        lambda: {"mysql": {"ok": True}, "opensearch": {"ok": False}},
    )
    response = _client().get("/api/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_health_deep_attaches_checks(monkeypatch):
    monkeypatch.setattr(
        health_mod,
        "_dependencies",
        lambda: {"mysql": {"ok": True}, "opensearch": {"ok": True}},
    )
    payload = _client().get("/api/health?deep=true").json()
    assert payload["status"] == "ok"
    assert set(payload["checks"]) == {"mysql", "opensearch"}


def test_request_id_is_echoed_and_generated():
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    with TestClient(app) as client:
        echoed = client.get("/ping", headers={"X-Request-ID": "abc123"})
        assert echoed.headers["X-Request-ID"] == "abc123"
        generated = client.get("/ping")
        assert generated.headers.get("X-Request-ID")
