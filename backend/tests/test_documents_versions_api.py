"""上传查重 / 重命名 / 版本管理 API 测试（TestClient + 内存假 repo + stub 索引）。

不依赖 GPU / MySQL / 网络：repo 层全部 monkeypatch 为内存实现，
RAGService.ensure_index 被 stub 并计数。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_service
from app.api.documents import router
from app.config import Settings
from app.db import get_db, get_db_optional, repository as repo
from app.rag.service import RAGService

BASE_TEXT = "".join(f"第{i}章，这是用于近重复检测的正文内容，足够长才能参与签名。" for i in range(40))


def _install_fake_repo(monkeypatch) -> dict:
    """把 repository 的版本/元数据函数替换为内存实现，返回内部状态。"""
    state = {"versions": {}, "next_id": 1, "renamed": []}

    def add_document_version(db, *, doc_relative_path, version_no, file_name,
                             original_name, size=0, note=""):
        if any(
            v["doc_relative_path"] == doc_relative_path and v["version_no"] == version_no
            for v in state["versions"].values()
        ):
            raise ValueError("duplicate version_no")
        vid = state["next_id"]
        state["next_id"] += 1
        state["versions"][vid] = {
            "id": vid,
            "doc_relative_path": doc_relative_path,
            "version_no": version_no,
            "file_name": file_name,
            "original_name": original_name,
            "size": size,
            "note": note,
            "created_at": datetime.now(),
        }
        return SimpleNamespace(**state["versions"][vid])

    def get_document_version(db, version_id):
        v = state["versions"].get(version_id)
        return SimpleNamespace(**v) if v else None

    def list_document_versions(db, doc_relative_path):
        return [
            dict(v)
            for v in sorted(state["versions"].values(), key=lambda x: -x["id"])
            if v["doc_relative_path"] == doc_relative_path
        ]

    def delete_document_version(db, version_id):
        return state["versions"].pop(version_id, None) is not None

    def version_no_exists(db, doc_relative_path, version_no):
        return any(
            v["doc_relative_path"] == doc_relative_path and v["version_no"] == version_no
            for v in state["versions"].values()
        )

    def rename_document_versions(db, old_path, new_path):
        n = 0
        for v in state["versions"].values():
            if v["doc_relative_path"] == old_path:
                v["doc_relative_path"] = new_path
                n += 1
        return n

    monkeypatch.setattr(repo, "add_document_version", add_document_version)
    monkeypatch.setattr(repo, "get_document_version", get_document_version)
    monkeypatch.setattr(repo, "list_document_versions", list_document_versions)
    monkeypatch.setattr(repo, "delete_document_version", delete_document_version)
    monkeypatch.setattr(repo, "version_no_exists", version_no_exists)
    monkeypatch.setattr(repo, "rename_document_versions", rename_document_versions)
    monkeypatch.setattr(repo, "rename_document_meta", lambda db, o, n: True)
    monkeypatch.setattr(repo, "delete_document_meta", lambda db, p: True)
    monkeypatch.setattr(repo, "delete_document_versions_all", lambda db, p: 0)
    return state


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    settings = Settings(data_dir=data, meta_dir=tmp_path / "meta")
    service = RAGService(settings)
    calls = {"ensure_index": 0}
    service.ensure_index = lambda **kw: calls.__setitem__("ensure_index", calls["ensure_index"] + 1)  # type: ignore[method-assign]

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_service] = lambda: service
    app.dependency_overrides[get_db] = lambda: object()
    app.dependency_overrides[get_db_optional] = lambda: object()
    state = _install_fake_repo(monkeypatch)
    client = TestClient(app)
    return SimpleNamespace(
        client=client, data=data, service=service, calls=calls, state=state, tmp_path=tmp_path
    )


def _upload(env, name: str, content: bytes, **form):
    return env.client.post(
        "/documents/upload",
        files={"files": (name, content, "application/octet-stream")},
        data=form or {"conflict_policy": "ask"},
    )


# ---------------- 上传 ----------------


def test_upload_no_conflict_ok(app_env):
    resp = _upload(app_env, "新书.txt", "内容一".encode("utf-8"))
    assert resp.status_code == 200
    assert resp.json()[0]["name"] == "新书.txt"
    assert (app_env.data / "新书.txt").is_file()
    assert app_env.calls["ensure_index"] == 1


def test_upload_dry_run_no_write(app_env):
    resp = _upload(app_env, "预检.txt", "内容".encode("utf-8"), dry_run="true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_conflict"] is False
    assert body["files"][0]["file_name"] == "预检.txt"
    assert not (app_env.data / "预检.txt").exists()


def test_upload_same_name_409(app_env):
    (app_env.data / "示例文档.txt").write_text("旧内容", encoding="utf-8")
    resp = _upload(app_env, "示例文档.txt", "新内容".encode("utf-8"))
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "upload_conflict"
    item = detail["files"][0]
    assert item["would_replace"] is True
    assert item["suggested_version_no"] == "v1"
    assert any(c["kind"] == "same_name" for c in item["conflicts"])
    assert (app_env.data / "示例文档.txt").read_text(encoding="utf-8") == "旧内容"


def test_upload_proceed_same_name_requires_version(app_env):
    (app_env.data / "a.txt").write_text("旧", encoding="utf-8")
    resp = _upload(app_env, "a.txt", "新".encode("utf-8"), conflict_policy="proceed")
    assert resp.status_code == 400
    assert "归档版本号" in resp.json()["detail"]


def test_upload_proceed_archives_old_version(app_env):
    (app_env.data / "a.txt").write_text("旧内容", encoding="utf-8")
    resp = _upload(
        app_env,
        "a.txt",
        "新内容".encode("utf-8"),
        conflict_policy="proceed",
        archive_version_no="v1",
    )
    assert resp.status_code == 200
    assert (app_env.data / "a.txt").read_text(encoding="utf-8") == "新内容"
    archived = app_env.tmp_path / "data_versions" / "a.txt" / "v1__a.txt"
    assert archived.read_text(encoding="utf-8") == "旧内容"
    rows = list(app_env.state["versions"].values())
    assert len(rows) == 1 and rows[0]["version_no"] == "v1"
    # 第二次同名重传：建议版本号 v2
    resp2 = _upload(app_env, "a.txt", "再新".encode("utf-8"))
    assert resp2.status_code == 409
    assert resp2.json()["detail"]["files"][0]["suggested_version_no"] == "v2"


def test_upload_identical_conflict(app_env):
    (app_env.data / "b.txt").write_text(BASE_TEXT, encoding="utf-8")
    resp = _upload(app_env, "另一本.txt", BASE_TEXT.encode("utf-8"))
    assert resp.status_code == 409
    kinds = [c["kind"] for c in resp.json()["detail"]["files"][0]["conflicts"]]
    assert "identical" in kinds


# ---------------- 重命名 ----------------


def test_rename_ok(app_env):
    (app_env.data / "旧名.txt").write_text("x", encoding="utf-8")
    resp = app_env.client.post(
        "/documents/rename", json={"relative_path": "旧名.txt", "new_name": "新名.txt"}
    )
    assert resp.status_code == 200
    assert (app_env.data / "新名.txt").is_file()
    assert not (app_env.data / "旧名.txt").exists()
    assert app_env.calls["ensure_index"] >= 1


def test_rename_same_name_400(app_env):
    (app_env.data / "a.txt").write_text("x", encoding="utf-8")
    resp = app_env.client.post(
        "/documents/rename", json={"relative_path": "a.txt", "new_name": "a.txt"}
    )
    assert resp.status_code == 400
    assert "原名相同" in resp.json()["detail"]


def test_rename_conflict_exact_and_normalized(app_env):
    (app_env.data / "a.txt").write_text("x", encoding="utf-8")
    (app_env.data / "示例文档.txt").write_text("y", encoding="utf-8")
    resp = app_env.client.post(
        "/documents/rename", json={"relative_path": "a.txt", "new_name": "示例文档.txt"}
    )
    assert resp.status_code == 409
    resp2 = app_env.client.post(
        "/documents/rename", json={"relative_path": "a.txt", "new_name": "示例 文档.txt"}
    )
    assert resp2.status_code == 409
    assert "重名" in resp2.json()["detail"]


def test_rename_missing_404(app_env):
    resp = app_env.client.post(
        "/documents/rename", json={"relative_path": "不存在.txt", "new_name": "x.txt"}
    )
    assert resp.status_code == 404


# ---------------- 版本管理 ----------------


def test_versions_list_empty_suggest_v1(app_env):
    (app_env.data / "书.txt").write_text("内容", encoding="utf-8")
    resp = app_env.client.get("/documents/versions", params={"path": "书.txt"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["current"] is not None
    assert body["versions"] == []
    assert body["suggested_version_no"] == "v1"


def test_versions_list_404_when_both_missing(app_env):
    resp = app_env.client.get("/documents/versions", params={"path": "没有.txt"})
    assert resp.status_code == 404


def test_archive_as_version_removes_source(app_env):
    (app_env.data / "主文档.pdf").write_text("主", encoding="utf-8")
    (app_env.data / "旧稿.txt").write_text("旧稿内容", encoding="utf-8")
    resp = app_env.client.post(
        "/documents/versions/archive",
        json={
            "source_path": "旧稿.txt",
            "target_path": "主文档.pdf",
            "version_no": "v1",
            "note": "初稿",
        },
    )
    assert resp.status_code == 200
    assert not (app_env.data / "旧稿.txt").exists()
    archived = app_env.tmp_path / "data_versions" / "主文档.pdf" / "v1__旧稿.txt"
    assert archived.is_file()
    body = resp.json()
    assert [v["version_no"] for v in body["versions"]] == ["v1"]
    assert body["suggested_version_no"] == "v2"
    assert all(d["name"] != "旧稿.txt" for d in body["documents"])


def test_archive_duplicate_version_409(app_env):
    (app_env.data / "主书.txt").write_text("主", encoding="utf-8")
    (app_env.data / "稿1.txt").write_text("一", encoding="utf-8")
    (app_env.data / "稿2.txt").write_text("二", encoding="utf-8")
    r1 = app_env.client.post(
        "/documents/versions/archive",
        json={"source_path": "稿1.txt", "target_path": "主书.txt", "version_no": "v1"},
    )
    assert r1.status_code == 200
    r2 = app_env.client.post(
        "/documents/versions/archive",
        json={"source_path": "稿2.txt", "target_path": "主书.txt", "version_no": "v1"},
    )
    assert r2.status_code == 409


def test_restore_swaps_versions(app_env):
    (app_env.data / "主书.txt").write_text("当前版本", encoding="utf-8")
    (app_env.data / "老稿.txt").write_text("老版本内容", encoding="utf-8")
    app_env.client.post(
        "/documents/versions/archive",
        json={"source_path": "老稿.txt", "target_path": "主书.txt", "version_no": "v1"},
    )
    version_id = next(iter(app_env.state["versions"]))
    resp = app_env.client.post(
        f"/documents/versions/{version_id}/restore",
        json={"new_version_no": "v2", "note": "被替换的当前版本"},
    )
    assert resp.status_code == 200
    assert (app_env.data / "主书.txt").read_text(encoding="utf-8") == "老版本内容"
    vdir = app_env.tmp_path / "data_versions" / "主书.txt"
    assert (vdir / "v2__主书.txt").read_text(encoding="utf-8") == "当前版本"
    body = resp.json()
    assert [v["version_no"] for v in body["versions"]] == ["v2"]


def test_restore_cross_extension_renames_doc(app_env):
    (app_env.data / "主书.pdf").write_text("PDF 当前版", encoding="utf-8")
    (app_env.data / "稿.txt").write_text("txt 历史版", encoding="utf-8")
    app_env.client.post(
        "/documents/versions/archive",
        json={"source_path": "稿.txt", "target_path": "主书.pdf", "version_no": "v1"},
    )
    version_id = next(iter(app_env.state["versions"]))
    resp = app_env.client.post(
        f"/documents/versions/{version_id}/restore",
        json={"new_version_no": "v2"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["new_relative_path"] == "主书.txt"
    assert (app_env.data / "主书.txt").read_text(encoding="utf-8") == "txt 历史版"
    assert not (app_env.data / "主书.pdf").exists()
    assert (app_env.tmp_path / "data_versions" / "主书.txt" / "v2__主书.pdf").is_file()


def test_delete_version_removes_file_and_row(app_env):
    (app_env.data / "主书.txt").write_text("主", encoding="utf-8")
    (app_env.data / "稿.txt").write_text("稿", encoding="utf-8")
    app_env.client.post(
        "/documents/versions/archive",
        json={"source_path": "稿.txt", "target_path": "主书.txt", "version_no": "v1"},
    )
    version_id = next(iter(app_env.state["versions"]))
    resp = app_env.client.delete(f"/documents/versions/{version_id}")
    assert resp.status_code == 200
    assert resp.json()["versions"] == []
    assert not (app_env.tmp_path / "data_versions" / "主书.txt" / "v1__稿.txt").exists()


def test_delete_version_route_order_not_swallowed(app_env):
    """回归：DELETE /documents/versions/{id} 不能被 DELETE /documents/{path:path} 吞掉。"""
    resp = app_env.client.delete("/documents/versions/999")
    assert resp.status_code == 404
    assert "版本不存在" in resp.json()["detail"]


def test_delete_document_purge_versions(app_env):
    (app_env.data / "主书.txt").write_text("主", encoding="utf-8")
    (app_env.data / "稿.txt").write_text("稿", encoding="utf-8")
    app_env.client.post(
        "/documents/versions/archive",
        json={"source_path": "稿.txt", "target_path": "主书.txt", "version_no": "v1"},
    )
    resp = app_env.client.delete(
        "/documents/主书.txt", params={"purge_versions": "true"}
    )
    assert resp.status_code == 200
    assert not (app_env.tmp_path / "data_versions" / "主书.txt").exists()
    assert not (app_env.data / "主书.txt").exists()
