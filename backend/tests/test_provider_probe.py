"""供应商探测核心逻辑（provider_probe）：掩码还原边界 + ping 不变量。

安全回归重点：掩码 Key 只能"原样回送到原地址"。改 host 或新 id 都必须拒绝
（评审发现：旧 `_validate_providers` 的 env 回退能把环境里的真实 Key 发往
任意 base_url——这里用测试钉死不变量）。所有测试不发真实网络请求。
"""

from __future__ import annotations

from app import provider_probe as pp


def _cfg(**over):
    base = {
        "id": "p_1",
        "type": "chat",
        "base_url": "https://api.example.com",
        "api_key": "sk-plain",
        "model": "m1",
    }
    base.update(over)
    return base


# ---------------- 掩码 Key 还原 ----------------


def test_restore_plain_key_passthrough():
    cfg, err = pp.restore_masked_key(_cfg(), [])
    assert err is None
    assert cfg["api_key"] == "sk-plain"


def test_restore_masked_same_id_and_host():
    saved = [_cfg(api_key="sk-real")]
    cfg, err = pp.restore_masked_key(
        _cfg(api_key="****real", base_url="https://api.example.com/v1"), saved
    )
    assert err is None
    assert cfg["api_key"] == "sk-real"


def test_restore_masked_rejects_host_change():
    saved = [_cfg(api_key="sk-real")]
    _, err = pp.restore_masked_key(
        _cfg(api_key="****real", base_url="https://evil.example.com"), saved
    )
    assert err is not None and "重新输入" in err


def test_restore_masked_rejects_unknown_id():
    _, err = pp.restore_masked_key(_cfg(id="p_new", api_key="****real"), [])
    assert err is not None


def test_probe_rejects_cloud_metadata_and_non_http_urls():
    out1 = pp.probe_models(_cfg(base_url="http://169.254.169.254/latest/meta-data"))
    assert out1["ok"] is False and "禁止访问" in out1["error"]
    out2 = pp.probe_chat(_cfg(base_url="file:///etc/passwd"))
    assert out2["ok"] is False and "http" in out2["error"]



# ---------------- probe_models 真实调用链 ----------------


def test_probe_models_calls_candidate_endpoints(monkeypatch):
    """不 mock probe_models 本身：确保候选端点函数名与调用点一致。"""
    from app import network, provider_candidates

    calls: list[str] = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "m1"}, {"name": "m2"}]}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers):
            calls.append(url)
            return _Resp()

    monkeypatch.setattr(
        network, "make_httpx_client", lambda **kw: _Client()
    )
    monkeypatch.setattr(
        provider_candidates,
        "models_candidates",
        lambda cfg: [("https://api.example.com/models", {"Authorization": "Bearer x"})],
    )
    out = pp.probe_models(_cfg())
    assert out["ok"] is True
    assert [m["id"] for m in out["models"]] == ["m1", "m2"]
    assert calls == ["https://api.example.com/models"]


# ---------------- probe_provider 编排 ----------------


def test_probe_provider_requires_model(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(
        pp, "probe_chat",
        lambda cfg, timeout=20.0: called.append("chat") or {"ok": True, "latency_ms": 1, "error": None},
    )
    monkeypatch.setattr(
        pp, "probe_models",
        lambda cfg, timeout=15.0: called.append("models") or {"ok": True, "models": [], "error": None},
    )
    out = pp.probe_provider(_cfg(model=""))
    assert out["ok"] is False and out["stage"] == "model"
    assert called == []  # 没有模型名：一个请求都不发


def test_probe_provider_key_rejected(monkeypatch):
    monkeypatch.setattr(pp, "probe_chat", lambda cfg, timeout=20.0: {"ok": True, "latency_ms": 1, "error": None})
    out = pp.probe_provider(_cfg(api_key="****x", id="p_new"), saved=[])
    assert out["stage"] == "key" and out["ok"] is False


def test_probe_provider_chat_failure_keeps_models(monkeypatch):
    monkeypatch.setattr(
        pp, "probe_models",
        lambda cfg, timeout=15.0: {"ok": True, "models": [{"id": "a"}, {"id": "b"}], "error": None},
    )
    monkeypatch.setattr(
        pp, "probe_chat",
        lambda cfg, timeout=20.0: {"ok": False, "latency_ms": 12, "error": "HTTP 404"},
    )
    out = pp.probe_provider(_cfg())
    assert out["ok"] is False and out["stage"] == "chat"
    assert out["error"] == "HTTP 404"
    assert out["models_count"] == 2


# ---------------- probe_chat 请求形状 ----------------


def test_probe_chat_uses_short_timeout_and_no_retry(monkeypatch):
    captured: dict = {}

    class _FakeLLM:
        def invoke(self, _):
            return object()

    def fake_build(cfg, **kw):
        captured.update(kw)
        return _FakeLLM()

    monkeypatch.setattr(pp, "build_chat_model", fake_build)
    out = pp.probe_chat(_cfg())
    assert out["ok"] is True and out["latency_ms"] >= 0
    assert captured["max_retries"] == 0
    assert captured["max_tokens"] == 64
    assert captured["timeout"] == 20.0


def test_probe_chat_reports_error(monkeypatch):
    class _Boom:
        def invoke(self, _):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(pp, "build_chat_model", lambda cfg, **kw: _Boom())
    out = pp.probe_chat(_cfg())
    assert out["ok"] is False and "connection refused" in out["error"]


# ---------------- speedtest ----------------


def test_speedtest_skips_ineligible_and_preserves_order(monkeypatch):
    called_keys: list[str] = []

    def fake_probe(cfg, timeout=15.0):
        called_keys.append(cfg["id"])
        return {"ok": True, "latency_ms": 5, "error": None}

    monkeypatch.setattr(pp, "probe_chat", fake_probe)
    saved = [
        _cfg(id="a"),
        _cfg(id="vision", type="vision"),
        _cfg(id="disabled", enabled=False),
        _cfg(id="nokey", api_key=""),
        _cfg(id="b"),
    ]
    results = pp.speedtest(saved)
    assert [r["id"] for r in results] == ["a", "nokey", "b"]
    assert sorted(called_keys) == ["a", "b"]  # 无 Key 不发请求
    nokey = next(r for r in results if r["id"] == "nokey")
    assert nokey["ok"] is False and "API Key" in nokey["error"]
