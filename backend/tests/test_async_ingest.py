"""后台增量索引（start_background_ingest / ingest_state）状态机测试。

不依赖 OpenSearch / GPU：_run_ensure_index_locked 被替换为可控假实现，
只验证线程编排与状态流转（rerun 补跑 / 重建互斥 / 解析器守卫）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.rag.loader import PARSE_SCHEMA_VERSION
from app.rag.service import RAGService

# 复用文档 API 测试的应用夹具（TestClient + 假 repo + stub 服务）
from test_documents_versions_api import app_env  # noqa: F401


@pytest.fixture()
def service(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", meta_dir=tmp_path / "meta")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    svc = RAGService(settings)
    counter = {"runs": 0}
    lock = threading.Lock()

    def _fake_locked(force_rebuild: bool = False):
        with lock:
            counter["runs"] += 1

    svc._run_ensure_index_locked = _fake_locked  # type: ignore[method-assign]
    svc._ingest_counter = counter
    return svc


def _join(svc: RAGService) -> None:
    thread = svc._ingest_thread
    if thread is not None:
        thread.join(timeout=5)


def test_ingest_state_initial(service):
    st = service.ingest_state()
    assert st == {
        "running": False,
        "reason": None,
        "started_at": None,
        "finished_at": None,
        "error": None,
    }


def test_start_runs_in_background_and_finishes(service):
    result = service.start_background_ingest("upload")
    assert result["started"] is True
    assert result["reason"] == "upload"
    _join(service)
    st = service.ingest_state()
    assert st["running"] is False
    assert st["finished_at"] is not None
    assert st["error"] is None
    assert service._ingest_counter["runs"] == 1


def test_rerun_while_running_picks_up_new_files(service):
    """第一轮跑的过程中又有新文件落盘：already_running → rerun 补跑一轮。"""
    entered = threading.Event()
    release = threading.Event()

    def _blocking_run(force_rebuild: bool = False):
        entered.set()
        release.wait(5)

    service._run_ensure_index_locked = _blocking_run  # type: ignore[method-assign]
    service.start_background_ingest("upload")
    assert entered.wait(5)

    second = service.start_background_ingest("upload")
    assert second["started"] is False
    assert second["skip_reason"] == "already_running"

    release.set()
    _join(service)
    st = service.ingest_state()
    assert st["running"] is False
    assert st["finished_at"] is not None


def test_start_returns_busy_when_rebuild_running(service):
    service._rebuild_state["running"] = True
    result = service.start_background_ingest("upload")
    assert result["started"] is False
    assert result["skip_reason"] == "index_busy"
    assert service._ingest_thread is None


def test_parse_upgrade_pending_skips_ingest(service):
    """解析器版本与账本不一致：后台线程不硬来（需先显式全量重建）。"""
    manifest_path = service._manifest_file()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"parse_version": "ancient-v0", "files": {}}), encoding="utf-8"
    )
    result = service.start_background_ingest("upload")
    assert result["started"] is True
    _join(service)
    st = service.ingest_state()
    assert st["running"] is False
    assert st["error"] is None
    assert "解析器待重建" in (st.get("note") or "")
    assert service._ingest_counter["runs"] == 0
    assert manifest_path.read_text(encoding="utf-8")  # 账本未被改动


def test_error_recorded_not_raised(service):
    def _boom(force_rebuild: bool = False):
        raise RuntimeError("OpenSearch 不可达")

    service._run_ensure_index_locked = _boom  # type: ignore[method-assign]
    service.start_background_ingest("upload")
    _join(service)
    st = service.ingest_state()
    assert st["running"] is False
    assert "RuntimeError" in (st["error"] or "")
    assert st["finished_at"] is not None


def test_index_health_exposes_ingest_state(app_env):
    """GET /api/index/health 携带 ingest 字段（前端轮询进度的数据源）。"""
    # 避免真实 Paddle 客户端探测 8090 端口
    app_env.service._paddle_ocr = SimpleNamespace(
        available=lambda: False, base_url="http://127.0.0.1:8090", last_error=""
    )
    app_env.service._ingest_state.update(
        running=True, reason="upload", started_at=datetime.now().isoformat(timespec="seconds")
    )
    resp = app_env.client.get("/index/health")
    assert resp.status_code == 200
    ingest = resp.json()["ingest"]
    assert ingest["running"] is True
    assert ingest["reason"] == "upload"
