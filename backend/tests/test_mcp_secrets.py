"""MCP 配置脱敏：读取只回掩码，保存按掩码保留真实值。"""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_agent_service
from app.api.advanced import router as advanced_router
from app.db import get_db
from app.mcp_secrets import (
    SECRET_MASK,
    is_secret_key,
    mask_mcp_servers,
    merge_mcp_secrets,
)


def test_secret_key_detection():
    assert is_secret_key("Authorization")
    assert is_secret_key("X-Api-Key")
    assert is_secret_key("GITHUB_TOKEN")
    assert is_secret_key("client_secret")
    assert not is_secret_key("X-Trace-Id")
    assert not is_secret_key("FOO")


def test_mask_servers_hides_secrets_without_mutating_input():
    servers = [
        {
            "id": "github",
            "type": "http",
            "headers": {
                "Authorization": "Bearer gho_secret",
                "X-Trace-Id": "trace-1",
            },
            "env": {"API_KEY": "sk-secret", "MODE": "prod"},
        }
    ]
    masked = mask_mcp_servers(servers)
    assert masked[0]["headers"]["Authorization"] == SECRET_MASK
    assert masked[0]["headers"]["X-Trace-Id"] == "trace-1"
    assert masked[0]["env"]["API_KEY"] == SECRET_MASK
    assert masked[0]["env"]["MODE"] == "prod"
    # 原对象不被改写
    assert servers[0]["headers"]["Authorization"] == "Bearer gho_secret"


def test_merge_keeps_old_secret_when_mask_is_echoed():
    existing = [
        {
            "id": "github",
            "headers": {"Authorization": "Bearer gho_secret"},
            "env": {"TOKEN": "t0ken"},
        }
    ]
    incoming = [
        {
            "id": "github",
            "headers": {"Authorization": SECRET_MASK, "X-New": "1"},
            "env": {"TOKEN": SECRET_MASK},
        }
    ]
    merged = merge_mcp_secrets(incoming, existing)
    assert merged[0]["headers"]["Authorization"] == "Bearer gho_secret"
    assert merged[0]["headers"]["X-New"] == "1"
    assert merged[0]["env"]["TOKEN"] == "t0ken"


def test_merge_replaces_new_value_and_removes_omitted_key():
    existing = [
        {
            "id": "github",
            "headers": {"Authorization": "Bearer old", "X-Old": "1"},
            "env": {},
        }
    ]
    incoming = [
        {"id": "github", "headers": {"Authorization": "Bearer new"}, "env": {}}
    ]
    merged = merge_mcp_secrets(incoming, existing)
    assert merged[0]["headers"]["Authorization"] == "Bearer new"
    assert "X-Old" not in merged[0]["headers"]


def test_merge_drops_mask_when_no_old_value():
    incoming = [{"id": "new", "headers": {"Authorization": SECRET_MASK}}]
    merged = merge_mcp_secrets(incoming, [])
    assert "Authorization" not in merged[0]["headers"]


class _FakeAgent:
    def mcp_status(self):
        return {}

    def refresh(self):
        pass

    def connect_mcp_servers(self, db, ids=None):
        return {"status": {}}


def test_mcp_api_masks_get_and_preserves_secret_on_put(monkeypatch):
    store = {
        "mcp_servers": json.dumps(
            [
                {
                    "id": "github",
                    "name": "GitHub",
                    "type": "http",
                    "url": "https://example.invalid/mcp",
                    "enabled": True,
                    "env": {},
                    "headers": {"Authorization": "Bearer gho_real"},
                }
            ]
        )
    }
    monkeypatch.setattr(
        "app.api.advanced.repo.get_meta", lambda db, key: store.get(key)
    )

    def _set_meta(db, key, value):
        store[key] = value

    monkeypatch.setattr("app.api.advanced.repo.set_meta", _set_meta)

    app = FastAPI()
    app.include_router(advanced_router)
    app.dependency_overrides[get_db] = lambda: object()
    app.dependency_overrides[get_agent_service] = lambda: _FakeAgent()
    client = TestClient(app)

    listed = client.get("/mcp").json()["servers"]
    assert listed[0]["headers"]["Authorization"] == SECRET_MASK

    # 前端把掩码原样回传：数据库里的真实值必须保留
    saved = client.put("/mcp", json={"servers": listed})
    assert saved.status_code == 200
    assert saved.json()["servers"][0]["headers"]["Authorization"] == SECRET_MASK
    persisted = json.loads(store["mcp_servers"])
    assert persisted[0]["headers"]["Authorization"] == "Bearer gho_real"

    # 用户改值则覆盖
    listed[0]["headers"]["Authorization"] = "Bearer gho_new"
    client.put("/mcp", json={"servers": listed})
    persisted = json.loads(store["mcp_servers"])
    assert persisted[0]["headers"]["Authorization"] == "Bearer gho_new"
