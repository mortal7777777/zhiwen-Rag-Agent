"""代理回退直连容错（app/network.py）的单元测试。

用本地 HTTPServer 模拟远端 API：系统代理指向必然拒绝连接的端口时，
make_httpx_client 应先试代理（ConnectError）再回退直连成功。
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

import pytest

from app.network import make_httpx_client


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_fallback_to_direct_when_proxy_dead(local_server, monkeypatch):
    """系统代理指向无监听端口（如 Clash 未启动）：请求应回退直连成功。"""
    monkeypatch.setattr(
        "app.network.system_proxy", lambda: "http://127.0.0.1:1"
    )
    port = local_server.server_address[1]
    with make_httpx_client(timeout=5) as client:
        resp = client.get(f"http://127.0.0.1:{port}/")
        assert resp.status_code == 200
        # 已回退后（进程内不再走代理）第二次请求同样成功
        resp2 = client.get(f"http://127.0.0.1:{port}/")
        assert resp2.status_code == 200


def test_direct_when_no_proxy(local_server, monkeypatch):
    """无系统代理配置：直接请求成功。"""
    monkeypatch.setattr("app.network.system_proxy", lambda: None)
    port = local_server.server_address[1]
    with make_httpx_client(timeout=5) as client:
        resp = client.get(f"http://127.0.0.1:{port}/")
        assert resp.status_code == 200


def test_proxy_used_when_alive(local_server, monkeypatch):
    """系统代理指向可达端口：请求走代理分支成功。"""
    # 用本地 server 自身充当"代理"（HTTP 代理能转发普通 GET）
    proxy_port = local_server.server_address[1]
    monkeypatch.setattr(
        "app.network.system_proxy", lambda: f"http://127.0.0.1:{proxy_port}"
    )
    port = local_server.server_address[1]
    with make_httpx_client(timeout=5) as client:
        resp = client.get(f"http://127.0.0.1:{port}/")
        assert resp.status_code == 200
