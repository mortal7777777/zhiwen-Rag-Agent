"""消息可见性过滤必须在 LIMIT 之前：工具轮不能把用户消息挤出窗口。"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import Conversation, Message
from app.db.repository import conversations as repo


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_visible_messages_survive_tool_heavy_tail():
    db = _session()
    conv = Conversation(title="t")
    db.add(conv)
    db.commit()
    db.refresh(conv)

    # 早期 10 条用户可见消息，后面 300 条工具轮行；SQLite 下 BigInteger
    # 主键不会自增，显式给 id
    next_id = 1
    for i in range(10):
        db.add(
            Message(
                id=next_id,
                conversation_id=conv.id,
                role="user",
                content=f"question {i}",
            )
        )
        next_id += 1
    for i in range(300):
        db.add(
            Message(
                id=next_id,
                conversation_id=conv.id,
                role="tool",
                content=f"tool {i}",
            )
        )
        next_id += 1
    db.add(
        Message(
            id=next_id,
            conversation_id=conv.id,
            role="assistant",
            content='{"__tool_calls__": []}',
        )
    )
    db.commit()

    rows = repo.list_messages(db, conv.id, limit=20)
    assert [r["content"] for r in rows] == [f"question {i}" for i in range(10)]


def test_get_message_scopes_by_conversation():
    db = _session()
    a = Conversation(title="a")
    b = Conversation(title="b")
    db.add_all([a, b])
    db.commit()
    db.refresh(a)
    db.refresh(b)
    msg = Message(id=1, conversation_id=a.id, role="user", content="hello")
    db.add(msg)
    db.commit()
    db.refresh(msg)

    assert repo.get_message(db, a.id, msg.id)["content"] == "hello"
    assert repo.get_message(db, b.id, msg.id) is None
