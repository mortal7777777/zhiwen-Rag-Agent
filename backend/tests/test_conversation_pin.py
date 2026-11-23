"""会话置顶（2026-11-19 P3·⑥）：repo 排序 + PATCH 端点。

不依赖 MySQL：SQLite 内存库 + Base.metadata.create_all（models 用了
with_variant 兼容 sqlite）；API 层用 TestClient + dependency_overrides。
"""

from __future__ import annotations

from datetime import datetime, timedelta

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
    # TestClient 在独立线程跑 app：check_same_thread=False + StaticPool
    # 让内存 SQLite 可跨线程共享
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_pin_moves_conversation_to_front():
    db = _session()
    old = repo.create_conversation(db, title="旧会话")
    new = repo.create_conversation(db, title="新会话")
    # 显式设定更新时间，避免同时间戳导致排序不稳定
    old.updated_at = datetime.now() - timedelta(days=3)
    new.updated_at = datetime.now()
    db.commit()

    # 未置顶：按更新时间倒序
    assert [c["id"] for c in repo.list_conversations(db)] == [new.id, old.id]

    # 置顶较旧的会话 → 排到最前
    pinned = repo.update_conversation(db, old.id, pinned=True)
    assert pinned is not None and pinned.pinned is True
    items = repo.list_conversations(db)
    assert [c["id"] for c in items] == [old.id, new.id]
    assert items[0]["pinned"] is True and items[1]["pinned"] is False

    # 取消置顶 → 回到时间序。注意：update 触发模型 onupdate=now，
    # old 的 updated_at 被刷新为"刚刚"——它在时间序里本来就该在最前，
    # 这与重命名的行为一致（动过的会话浮到顶部）
    repo.update_conversation(db, old.id, pinned=False)
    items = repo.list_conversations(db)
    assert [c["id"] for c in items] == [old.id, new.id]
    assert all(c["pinned"] is False for c in items)


def test_pin_only_patch_keeps_title():
    """只传 pinned：标题不变（title=None 跳过重命名）。"""
    db = _session()
    conv = repo.create_conversation(db, title="保持不变")
    db.commit()
    updated = repo.update_conversation(db, conv.id, title=None, pinned=True)
    assert updated.title == "保持不变"
    assert updated.pinned is True


def test_patch_endpoint_pin_roundtrip():
    """PATCH /conversations/{id} {pinned} 端到端：返回体带 pinned。"""
    db = _session()
    conv = repo.create_conversation(db, title="端点测试")
    db.commit()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_agent_service] = lambda: object()
    client = TestClient(app)

    resp = client.patch(f"/conversations/{conv.id}", json={"pinned": True})
    assert resp.status_code == 200
    assert resp.json()["pinned"] is True
    assert resp.json()["title"] == "端点测试"  # 只改置顶不动标题

    resp2 = client.patch(f"/conversations/{conv.id}", json={"pinned": False})
    assert resp2.status_code == 200
    assert resp2.json()["pinned"] is False

    # 列表也带 pinned 字段（前端渲染置顶组的数据源）
    listing = client.get("/conversations")
    assert listing.status_code == 200
    assert listing.json()[0]["pinned"] is False

    resp404 = client.patch("/conversations/999", json={"pinned": True})
    assert resp404.status_code == 404
