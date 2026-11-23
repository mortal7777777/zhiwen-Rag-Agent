"""会话级 fork（6.9）：复制目标消息（含）之前的历史，保留提示词锁、重置摘要。"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import conversations as conversations_api
from app.api.conversations import router
from app.api.deps import get_agent_service
from app.db import get_db
from app.db import models  # noqa: F401 - 注册全部表
from app.db import repository as repo
from app.db.database import Base


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _source(db):
    """源会话：user/assistant(工具轮)/tool/user/assistant，带提示词锁与摘要。"""
    conv = repo.create_conversation(
        db, title="原会话", template_id=7, project_dir="D:/work"
    )
    conv.system_prompt = "系统提示"
    conv.prompt_bundle = json.dumps({"template_version_no": 6}, ensure_ascii=False)
    conv.prompt_bundle_hash = "hash-abc"
    conv.prompt_locked_at = datetime(2026, 11, 21, 10, 0, 0)
    conv.summary = "旧摘要"
    conv.summary_up_to_id = 3
    db.commit()

    user1 = repo.add_message(db, conv.id, "user", "第一个问题")
    # 真实落库顺序：user → 工具轮（tool 行）→ 最终 assistant 回答
    tool_row = repo.add_message(db, conv.id, "tool", '{"summary": "ok"}')
    assistant1 = repo.add_message(
        db,
        conv.id,
        "assistant",
        "第一个回答",
        tool_trace=[{"name": "read_file", "summary": "ok"}],
        sources=[{"index": 1, "type": "kb", "content": "x"}],
    )
    repo.add_message(db, conv.id, "user", "第二个问题")
    repo.add_message(db, conv.id, "assistant", "第二个回答")
    return conv, user1, assistant1, tool_row


def test_fork_copies_messages_up_to_target_inclusive():
    db = _session()
    conv, _user1, assistant1, _tool_row = _source(db)

    result = repo.fork_conversation(db, conv.id, assistant1.id)
    assert result is not None
    forked = result["conversation"]

    assert forked.id != conv.id
    assert forked.title == "原会话（分支）"
    assert result["message_count"] == 3
    # 6.9 来源元数据：直接指向源会话与目标消息
    assert forked.forked_from_conversation_id == conv.id
    assert forked.forked_from_message_id == assistant1.id

    rows = repo.list_messages_with_id(db, forked.id)
    assert [r["role"] for r in rows] == ["user", "tool", "assistant"]
    assert [r["content"] for r in rows] == [
        "第一个问题",
        '{"summary": "ok"}',
        "第一个回答",
    ]
    # JSON 列按原样复制（字节一致，重建历史前缀才与源会话一致）
    assert json.loads(rows[2]["tool_trace"]) == [
        {"name": "read_file", "summary": "ok"}
    ]
    # 目标消息之后的问答不在分支里
    assert all("第二个" not in r["content"] for r in rows)


def test_fork_preserves_template_workspace_and_prompt_lock():
    db = _session()
    conv, _user1, assistant1, _tool_row = _source(db)

    forked = repo.fork_conversation(db, conv.id, assistant1.id)["conversation"]

    assert forked.template_id == 7
    assert forked.project_dir == "D:/work"
    assert forked.system_prompt == "系统提示"
    assert forked.prompt_bundle == conv.prompt_bundle
    assert forked.prompt_bundle_hash == "hash-abc"
    assert forked.prompt_locked_at == datetime(2026, 11, 21, 10, 0, 0)
    # 摘要因消息 id 全变而失效：重置为空
    assert forked.summary is None
    assert (forked.summary_up_to_id or 0) == 0
    # 源会话不受影响
    source = repo.get_conversation(db, conv.id)
    assert source.summary == "旧摘要" and source.summary_up_to_id == 3
    assert source.title == "原会话"

    # 列表接口也带来源字段（前端会话栏渲染"分支自…"的数据源）
    listed = {c["id"]: c for c in repo.list_conversations(db)}
    assert listed[forked.id]["forked_from_conversation_id"] == conv.id
    assert listed[forked.id]["forked_from_message_id"] == assistant1.id
    assert listed[conv.id]["forked_from_conversation_id"] is None


def test_fork_rejects_foreign_or_missing_message():
    db = _session()
    conv, _user1, _assistant1, _tool_row = _source(db)
    other = repo.create_conversation(db, title="别的会话")
    other_msg = repo.add_message(db, other.id, "user", "别处的问题")

    assert repo.fork_conversation(db, conv.id, other_msg.id) is None
    assert repo.fork_conversation(db, conv.id, 999999) is None
    assert repo.fork_conversation(db, 999999, other_msg.id) is None


def test_fork_endpoint_roundtrip():
    db = _session()
    conv, user1, _assistant1, _tool_row = _source(db)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_agent_service] = lambda: object()
    client = TestClient(app)

    resp = client.post(
        f"/conversations/{conv.id}/fork", json={"message_id": user1.id}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "原会话（分支）"
    assert body["template_id"] == 7
    assert body["project_dir"] == "D:/work"
    assert body["prompt_bundle_hash"] == "hash-abc"
    assert body["message_count"] == 1
    assert body["id"] != conv.id
    assert body["forked_from_conversation_id"] == conv.id
    assert body["forked_from_message_id"] == user1.id

    missing = client.post(
        f"/conversations/{conv.id}/fork", json={"message_id": 999999}
    )
    assert missing.status_code == 404

    no_conv = client.post("/conversations/999999/fork", json={"message_id": user1.id})
    assert no_conv.status_code == 404


def test_fork_endpoint_rejects_running_conversation(monkeypatch):
    db = _session()
    conv, user1, _assistant1, _tool_row = _source(db)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_agent_service] = lambda: object()
    client = TestClient(app)

    monkeypatch.setattr(
        conversations_api, "is_conversation_active", lambda _cid: True
    )
    resp = client.post(
        f"/conversations/{conv.id}/fork", json={"message_id": user1.id}
    )
    assert resp.status_code == 409
