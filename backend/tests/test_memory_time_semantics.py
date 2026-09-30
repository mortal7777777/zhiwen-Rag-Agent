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


def test_status_change_does_not_refresh_updated_at(db):
    """归档/恢复是管理动作，不是内容编辑：不得把旧记忆顶到"最近"位置。"""
    repo.update_memory(db, 1, status="archived")
    m = db.get(Memory, 1)
    assert m.status == "archived"
    assert m.updated_at == OLD
    assert m.created_at == OLD


TIE = datetime(2026, 9, 30, 9, 39, 53)


def test_list_memories_ordering_is_deterministic_under_ties(db):
    """updated_at 全并列时（批量恢复/合并的产物）排序必须仍然确定。

    历史事故：44 行 updated_at 相同，list_memories 返回顺序由 DB 决定，
    "最近记忆兜底"取到的是两条随机的旧记忆。
    """
    for mid, created in [(2, datetime(2026, 8, 1)), (3, datetime(2026, 9, 20)),
                         (4, datetime(2026, 9, 10))]:
        db.add(
            Memory(
                id=mid,
                content=f"事实 {mid}",
                category="other",
                status="active",
                hit_count=0,
                created_at=created,
                updated_at=TIE,          # 三行时间戳完全相同
            )
        )
    db.commit()

    # 默认（updated）：并列时按 id 降序兜底 → 确定性，不再是任意顺序
    assert [m["id"] for m in repo.list_memories(db)][:3] == [4, 3, 2]

    # order="created"：按录入时间降序，与 updated_at 无关（"最近的事实"语义）
    # 09-20(#3) > 09-10(#4) > 08-18(#1，夹具行) > 08-01(#2)
    created_order = [m["id"] for m in repo.list_memories(db, order="created")]
    assert created_order[:3] == [3, 4, 1]
    assert created_order[3] == 2


def test_list_all_memories_has_id_tiebreaker(db):
    """整合取全量时同样不能靠并列时间戳决定谁被 limit 截断。"""
    for mid in (5, 6):
        db.add(
            Memory(
                id=mid, content=f"事实 {mid}", category="other", status="archived",
                hit_count=0, created_at=datetime(2026, 9, 1), updated_at=TIE,
            )
        )
    db.commit()
    ids = [m["id"] for m in repo.list_all_memories(db)]
    assert ids[:3] == [6, 5, 1]           # TIE 组内按 id 降序，然后才是 08-18 那条


def test_update_memory_returns_none_for_missing_row(db):
    assert repo.update_memory(db, 999, content="x") is None
