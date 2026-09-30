"""记忆时间字段语义单测（回归锁定）。

背景：Memory.updated_at 曾带 onupdate=datetime.now，而 bump_memory_hits 每次召回
都会改 hit_count/last_hit_at → 触发 updated_at 刷新。于是"最后被召回"被当成
"最后被陈述"：
- 整合提示词假设"旧记忆在前、新记忆在后"，而排序用的 updated_at 已被召回污染；
- AGENTS.md 导出按 updated_at 取"最新 N 条"，同样取错。

这里锁死三条不变量：召回不改内容时间、编辑才改、首次入库时间永不变。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import repository as repo
from app.db.models import Memory

OLD = datetime(2026, 8, 18, 2, 17, 7)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    # 只建记忆表：Memory 用 BigInteger 主键，SQLite 不自增，用例里显式给 id。
    Memory.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    session.add(
        Memory(
            id=1,
            content="用户偏好准确直接的交流风格",
            category="preference",
            status="active",
            hit_count=0,
            created_at=OLD,
            updated_at=OLD,
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()


def test_bump_memory_hits_does_not_touch_updated_at(db):
    """召回累加命中数不得改写"内容最后修改时间"（这是污染源头）。"""
    repo.bump_memory_hits(db, [1])
    m = db.get(Memory, 1)
    assert m.hit_count == 1
    assert m.last_hit_at is not None   # 衰减依据走独立字段
    assert m.updated_at == OLD         # 关键：没被召回刷新
    assert m.created_at == OLD


def test_update_memory_refreshes_updated_at_only_on_real_change(db):
    repo.update_memory(db, 1, content="用户偏好准确直接的交流风格")  # 传入同值
    assert db.get(Memory, 1).updated_at == OLD   # 无净变更 → 不算编辑

    repo.update_memory(db, 1, content="用户偏好准确直接、基于实证的交流风格")
    m = db.get(Memory, 1)
    assert m.content == "用户偏好准确直接、基于实证的交流风格"
    assert m.updated_at > OLD          # 真编辑 → 刷新
    assert m.created_at == OLD         # 首次入库时间永不变（新旧判定的依据）


def test_status_change_refreshes_updated_at_but_not_created_at(db):
    repo.update_memory(db, 1, status="archived")
    m = db.get(Memory, 1)
    assert m.status == "archived"
    assert m.updated_at > OLD
    assert m.created_at == OLD


def test_update_memory_returns_none_for_missing_row(db):
    assert repo.update_memory(db, 999, content="x") is None
