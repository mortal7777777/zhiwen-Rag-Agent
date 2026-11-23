"""对话内「下一步建议」：解析/缓存/关闭开关/无回答时为空。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import suggestions as suggestions_api
from app.api.deps import get_agent_service
from app.api.suggestions import router
from app.db import get_db
from app.db import models  # noqa: F401 - 注册全部表
from app.db import repository as repo
from app.db.database import Base


@pytest.fixture(autouse=True)
def _clear_cache():
    suggestions_api._NEXT_CACHE.clear()
    yield
    suggestions_api._NEXT_CACHE.clear()


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class _Chat:
    def __init__(self, responses=None, error: Exception | None = None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        content = self.responses.pop(0) if self.responses else "[]"
        return SimpleNamespace(content=content)


def _client(db, chat: _Chat, **settings_overrides):
    settings = SimpleNamespace(
        **{
            "next_suggestions_enabled": True,
            "next_suggestions_cache_minutes": 30,
            **settings_overrides,
        }
    )
    agent = SimpleNamespace(settings=settings, title_chat=chat)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_agent_service] = lambda: agent
    return TestClient(app)


def _conversation(db, *, with_assistant: bool = True):
    conv = repo.create_conversation(db, title="建议测试")
    repo.add_message(db, conv.id, "user", "帮我总结 RAG 的流程")
    if with_assistant:
        repo.add_message(
            db,
            conv.id,
            "assistant",
            "RAG 分为检索、增强、生成三步。",
            tool_trace=[{"name": "knowledge_base_search", "summary": "命中 2 条"}],
        )
    return conv


def test_next_suggestions_llm_then_cache():
    db = _session()
    conv = _conversation(db)
    chat = _Chat(['["展开讲讲检索步骤", "给个例子", "整理成清单"]'])
    client = _client(db, chat)

    first = client.post(f"/conversations/{conv.id}/next-suggestions")
    assert first.status_code == 200
    body = first.json()
    assert body["items"] == ["展开讲讲检索步骤", "给个例子", "整理成清单"]
    assert body["source"] == "llm"
    assert chat.calls == 1

    second = client.post(f"/conversations/{conv.id}/next-suggestions")
    assert second.json()["source"] == "cache"
    assert second.json()["items"] == body["items"]
    assert chat.calls == 1


def test_next_suggestions_parses_fenced_and_lines():
    db = _session()
    conv = _conversation(db)
    chat = _Chat(["```json\n[\"再深入一点\", \"举个例子\", \"换个说法\"]\n```"])
    body = _client(db, chat).post(
        f"/conversations/{conv.id}/next-suggestions"
    ).json()
    assert body["items"] == ["再深入一点", "举个例子", "换个说法"]

    suggestions_api._NEXT_CACHE.clear()
    conv2 = _conversation(db)
    chat2 = _Chat(["- 对比两种方案\n- 给出推荐\n- 说明风险"])
    body2 = _client(db, chat2).post(
        f"/conversations/{conv2.id}/next-suggestions"
    ).json()
    assert body2["items"] == ["对比两种方案", "给出推荐", "说明风险"]


def test_next_suggestions_failure_and_disabled_are_empty():
    db = _session()
    conv = _conversation(db)
    failing = _Chat(error=RuntimeError("provider down"))
    body = _client(db, failing).post(
        f"/conversations/{conv.id}/next-suggestions"
    ).json()
    assert body == {"items": [], "source": "none"}

    disabled = _Chat(['["不该调用"]'])
    body2 = _client(
        db, disabled, next_suggestions_enabled=False
    ).post(f"/conversations/{conv.id}/next-suggestions").json()
    assert body2 == {"items": [], "source": "disabled"}
    assert disabled.calls == 0


def test_next_suggestions_without_assistant_or_conversation():
    db = _session()
    conv = _conversation(db, with_assistant=False)
    chat = _Chat(['["不该调用"]'])
    client = _client(db, chat)

    body = client.post(f"/conversations/{conv.id}/next-suggestions").json()
    assert body["items"] == [] and body["source"] == "none"
    assert chat.calls == 0

    missing = client.post("/conversations/999999/next-suggestions")
    assert missing.status_code == 404
