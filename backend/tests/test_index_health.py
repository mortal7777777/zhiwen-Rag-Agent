"""索引健康：manifest 指纹 vs 当前目录的差异（增量重建的输入）。"""

from __future__ import annotations

from pathlib import Path

from app.api.documents import index_health as index_health_endpoint
from app.api.documents import refresh_index as refresh_index_endpoint
from app.rag.loader import PARSE_SCHEMA_VERSION
from app.rag.service.docs import DocsMixin


class _FakeService:
    def __init__(self, old: dict, current: dict, manifest: Path) -> None:
        self.old = old
        self.current = current
        self.manifest = manifest
        self.ensured = 0

    def ingest_state(self):
        return {
            "running": False,
            "reason": None,
            "started_at": None,
            "finished_at": None,
            "error": None,
        }

    def _read_manifest(self):
        return {
            "files": self.old,
            "child_count": 3,
            "parent_count": 2,
            "parse_version": PARSE_SCHEMA_VERSION,
        }

    def _compute_file_fingerprints(self):
        return self.current

    def _manifest_file(self):
        return self.manifest

    def index_status(self):
        return {
            "index_name": "kb",
            "physical_index": "kb_1",
            "doc_count": 10,
            "chunk_count": 3,
            "parent_count": 2,
            "data_dir": "data",
            "error": None,
        }

    def index_health(self):
        return DocsMixin.index_health(self)

    def rebuild_state(self):
        return {"running": False, "started_at": None, "finished_at": None, "error": None}

    def ensure_index(self):
        self.ensured += 1


def test_index_health_diffs_manifest_and_directory(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    svc = _FakeService(
        old={"same.md": "h1", "changed.md": "h1", "gone.md": "h1"},
        current={"same.md": "h1", "changed.md": "h2", "new.md": "h1"},
        manifest=manifest,
    )

    health = svc.index_health()

    assert health["added_files"] == ["new.md"]
    assert health["changed_files"] == ["changed.md"]
    assert health["missing_files"] == ["gone.md"]
    assert health["tracked_files"] == 3
    assert health["current_files"] == 3
    assert health["stale"] is True
    assert health["last_built_at"]
    assert health["chunk_count"] == 3


def test_index_health_clean_state(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    svc = _FakeService(
        old={"a.md": "h1"},
        current={"a.md": "h1"},
        manifest=manifest,
    )
    health = svc.index_health()
    assert health["stale"] is False
    assert health["added_count"] == health["changed_count"] == health["missing_count"] == 0


def test_index_health_endpoint_and_refresh(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    svc = _FakeService(
        old={},
        current={"a.md": "h1"},
        manifest=manifest,
    )
    assert index_health_endpoint(svc)["added_files"] == ["a.md"]
    out = refresh_index_endpoint(svc)
    assert svc.ensured == 1
    assert out["current_files"] == 1
