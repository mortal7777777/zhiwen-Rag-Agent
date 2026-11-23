"""目录选择器本机校验：直连回环 / nginx 反代 / 公网伪造三种来源。"""

from starlette.requests import Request

from app.api.fs import _client_is_local


def _req(client_host: str, headers: dict | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/fs/browse",
            "headers": raw,
            "client": (client_host, 12345),
        }
    )


def test_loopback_client_allowed():
    assert _client_is_local(_req("127.0.0.1"))
    assert _client_is_local(_req("::1"))
    assert _client_is_local(_req("localhost"))


def test_nginx_private_peer_trusts_loopback_x_real_ip():
    assert _client_is_local(_req("172.18.0.5", {"X-Real-IP": "127.0.0.1"}))
    assert _client_is_local(_req("172.18.0.5", {"X-Real-IP": "::1"}))


def test_private_peer_without_loopback_source_denied():
    assert not _client_is_local(_req("172.18.0.5"))
    assert not _client_is_local(_req("172.18.0.5", {"X-Real-IP": "8.8.8.8"}))


def test_public_peer_cannot_spoof_x_real_ip():
    assert not _client_is_local(_req("8.8.8.8", {"X-Real-IP": "127.0.0.1"}))


def test_require_local_or_token_blocks_remote_when_token_unset(monkeypatch):
    from types import SimpleNamespace
    import pytest
    from fastapi import HTTPException
    import app.config as cfg_mod
    from app.api.fs import require_local_or_token

    monkeypatch.setattr(cfg_mod, "get_settings", lambda: SimpleNamespace(api_token=""))
    require_local_or_token(_req("127.0.0.1"))
    with pytest.raises(HTTPException) as exc:
        require_local_or_token(_req("192.168.1.50"))
    assert exc.value.status_code == 403

    monkeypatch.setattr(cfg_mod, "get_settings", lambda: SimpleNamespace(api_token="secret"))
    require_local_or_token(_req("192.168.1.50"))

