"""会话工作区（project_dir）：POST/PATCH/GET 端点与 repo 列表回显。

工作区复用既有 conversations.project_dir（列与基线迁移早已存在）；
Web 端通过会话 API 读写它来做「按工作区分类会话」。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.conversations import router
from app.api.deps import get_agent_service
from app.db import models  # noqa: F401 - 注册全部表
from app.db.database import Base
from app.db import repository as repo
from app.db import get_db


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _client(db) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_agent_service] = lambda: object()
    return TestClient(app)


def test_repo_list_returns_project_dir():
    db = _session()
    repo.create_conversation(db, title="有工作区", project_dir=r"D:\proj\demo")
    repo.create_conversation(db, title="无工作区")
    db.commit()

    items = repo.list_conversations(db)
    by_title = {c["title"]: c for c in items}
    assert by_title["有工作区"]["project_dir"] == r"D:\proj\demo"
    assert by_title["无工作区"]["project_dir"] is None


def test_create_endpoint_echoes_project_dir():
    db = _session()
    client = _client(db)

    resp = client.post(
        "/conversations",
        json={"title": "新建", "project_dir": r"D:\proj\demo"},
    )
    assert resp.status_code == 200
    assert resp.json()["project_dir"] == r"D:\proj\demo"

    listing = client.get("/conversations")
    assert listing.status_code == 200
    assert listing.json()[0]["project_dir"] == r"D:\proj\demo"


def test_patch_sets_and_clears_project_dir():
    db = _session()
    conv = repo.create_conversation(db, title="切换工作区")
    db.commit()
    client = _client(db)

    resp = client.patch(
        f"/conversations/{conv.id}", json={"project_dir": r"D:\proj\demo"}
    )
    assert resp.status_code == 200
    assert resp.json()["project_dir"] == r"D:\proj\demo"

    # 只改工作区不动标题
    assert resp.json()["title"] == "切换工作区"

    # 空串 = 清除（repo 把 "" 归一化为 None）；null = 不修改
    unchanged = client.patch(f"/conversations/{conv.id}", json={"project_dir": None})
    assert unchanged.status_code == 200
    assert unchanged.json()["project_dir"] == r"D:\proj\demo"

    cleared = client.patch(f"/conversations/{conv.id}", json={"project_dir": ""})
    assert cleared.status_code == 200
    assert cleared.json()["project_dir"] is None
