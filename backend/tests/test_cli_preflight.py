"""zhiwen 启动预检：后端未启动时给出明确指引，而不是进 REPL 后报连接错误。"""

from __future__ import annotations

import sys

import pytest

import cli_agent
from cli import http as cli_http


def test_check_backend_none_on_connection_error(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(cli_http, "http_json", boom)
    assert cli_http.check_backend("http://127.0.0.1:9") is None


def test_check_backend_returns_health_payload(monkeypatch):
    monkeypatch.setattr(cli_http, "http_json", lambda *a, **k: {"status": "ok"})
    assert cli_http.check_backend("http://127.0.0.1:8000") == {"status": "ok"}


def test_main_exits_with_startup_hint_when_backend_down(monkeypatch, capsys):
    monkeypatch.setattr(cli_agent, "check_backend", lambda base: None)
    monkeypatch.setattr(sys, "argv", ["cli_agent.py"])
    with pytest.raises(SystemExit) as exc:
        cli_agent.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "后端未启动或不可达" in out
    assert "start_all.bat" in out
