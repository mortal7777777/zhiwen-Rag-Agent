"""容器入口迁移：旧库（有表无版本表）必须先 stamp，新库直接 upgrade。"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from docker_migrate import needs_stamp


def _engine():
    return create_engine("sqlite://")


def test_fresh_database_does_not_need_stamp():
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE conversations (id INTEGER PRIMARY KEY)"))
    # 只有业务表、没有 alembic_version → 旧库
    assert needs_stamp(engine) is True


def test_empty_database_does_not_need_stamp():
    assert needs_stamp(_engine()) is False


def test_versioned_database_does_not_need_stamp():
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE conversations (id INTEGER PRIMARY KEY)"))
        conn.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
    assert needs_stamp(engine) is False
