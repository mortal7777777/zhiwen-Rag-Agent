"""文档查看器后端链路测试：只读文件服务 + relative_path 元数据透传。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_service
from app.api.documents import router
from app.agent.tools import extract_sources
from app.rag.loader import load_documents


class _FakeSettings:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.meta_dir = data_dir.parent / "meta"


class _FakeService:
    def __init__(self, data_dir):
        self.settings = _FakeSettings(data_dir)


def _make_client(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.md").write_text("# hello", encoding="utf-8")
    (tmp_path / "sub" / "b.pdf").write_bytes(b"%PDF-1.4 fake-bytes")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_service] = lambda: _FakeService(tmp_path)
    return TestClient(app)


def test_file_endpoint_serves_bytes_with_media_type(tmp_path):
    client = _make_client(tmp_path)
    resp = client.get("/documents/file/sub/b.pdf")
    assert resp.status_code == 200
    assert resp.content == b"%PDF-1.4 fake-bytes"
    assert resp.headers["content-type"].startswith("application/pdf")


def test_file_endpoint_text_suffix(tmp_path):
    client = _make_client(tmp_path)
    resp = client.get("/documents/file/a.md")
    assert resp.status_code == 200
    assert "hello" in resp.text


def test_file_endpoint_rejects_traversal(tmp_path):
    client = _make_client(tmp_path)
    resp = client.get("/documents/file/..%2f..%2fsecret.txt")
    assert resp.status_code == 400
    assert "非法路径" in resp.json()["detail"]


def test_file_endpoint_missing_file(tmp_path):
    client = _make_client(tmp_path)
    resp = client.get("/documents/file/nope.pdf")
    assert resp.status_code == 404


def test_loader_sets_relative_path(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text("# 标题\n\n内容", encoding="utf-8")
    docs = load_documents(tmp_path)
    assert docs
    assert all(d.metadata.get("relative_path") == "docs/note.md" for d in docs)


def test_extract_sources_passes_relative_path_and_images():
    result = {
        "results": [
            {
                "type": "kb",
                "content": "片段",
                "source": "a.pdf",
                "page": 3,
                "relative_path": "sub/a.pdf",
                "index": 1,
                "images": [{"id": "p0003_img0", "path": "ocr_cache/a/p0003_img0.png"}],
            }
        ]
    }
    sources = extract_sources(result)
    assert sources[0]["relative_path"] == "sub/a.pdf"
    assert sources[0]["page"] == 3
    assert sources[0]["images"][0]["id"] == "p0003_img0"


def test_image_endpoint_only_serves_ocr_cache(tmp_path):
    client = _make_client(tmp_path)
    img_dir = tmp_path.parent / "meta" / "ocr_cache" / "doc"
    img_dir.mkdir(parents=True)
    (img_dir / "p1.png").write_bytes(b"png-bytes")
    ok = client.get("/images", params={"path": "ocr_cache/doc/p1.png"})
    assert ok.status_code == 200 and ok.content == b"png-bytes"
    bad = client.get("/images", params={"path": "../secret.png"})
    assert bad.status_code == 404
