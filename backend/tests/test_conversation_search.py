"""会话搜索：标题 / 工作区 / 消息正文（repo 与 HTTP 端点）。"""

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
from app.db.models import Message
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


def _seed(db):
    titled = repo.create_conversation(db, title="Python 性能排查")
    body = repo.create_conversation(db, title="闲聊")
    workspace = repo.create_conversation(
        db, title="工作区会话", project_dir=r"D:\proj\rag_demo"
    )
    # SQLite 下 BigInteger 主键不会自增，显式给 id（同 test_message_visibility）
    messages = [
        (body.id, "user", "帮我看看 Python 的缓存命中率"),
        (body.id, "assistant", "Python 缓存命中率 95%，已经很稳"),
        (body.id, "tool", "Python 工具输出不应参与搜索"),
        (workspace.id, "user", "无关内容"),
    ]
    for index, (conversation_id, role, content) in enumerate(messages, start=1):
        db.add(
            Message(
                id=index,
                conversation_id=conversation_id,
                role=role,
                content=content,
            )
        )
    db.commit()
    return titled, body, workspace


def test_repo_searches_title_workspace_and_messages():
    db = _session()
    _seed(db)

    results = repo.search_conversations(db, "Python")
    by_title = {item["title"]: item for item in results}
    assert set(by_title) == {"Python 性能排查", "闲聊"}
    assert by_title["Python 性能排查"]["match_count"] == 0
    assert by_title["Python 性能排查"]["snippet"] == ""
    assert by_title["闲聊"]["match_count"] == 2
    assert "缓存命中率" in by_title["闲聊"]["snippet"]
    # tool 消息不参与正文搜索
    assert by_title["闲聊"]["match_count"] == 2

    ws_results = repo.search_conversations(db, "rag_demo")
    assert [item["title"] for item in ws_results] == ["工作区会话"]

    assert repo.search_conversations(db, "   ") == []


def test_search_endpoint_returns_snippets_and_validates_query():
    db = _session()
    _seed(db)
    client = _client(db)

    resp = client.get("/conversations/search", params={"q": "缓存命中率"})
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload) == 1
    assert payload[0]["title"] == "闲聊"
    assert payload[0]["match_count"] == 2
    assert "缓存命中率" in payload[0]["snippet"]

    missing = client.get("/conversations/search")
    assert missing.status_code == 422
    empty = client.get("/conversations/search", params={"q": ""})
    assert empty.status_code == 422
