"""Alembic 收编（_legacy_migrate_if_unmanaged）单元 + 集成测试。

双轨收敛（2026-11-19）语义：
- 未纳管旧库：_migrate 补列成功 → stamp 基线 → upgrade head（基线之后的新列
  由 revision 提供，如 conversations.pinned）；upgrade 失败清标记待重试；
- 已纳管库：跳过 _migrate，但补跑 pending revisions（stamp 在旧版本的库自动追平）；
- 全新库：create_all 后由 _activate_engine 直接 stamp head（结构==模型==head）。

_migrate 是 MySQL 专属 DDL，单测用假实现注入；集成用例用文件 SQLite 走真
alembic upgrade（env.py 经 ALEMBIC_DATABASE_URL 注入指向同一文件库）。
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect as sa_inspect, text

from app.db import database

BASELINE = "6018474f2912"
HEAD = "c4d8e2f1a6b3"  # conversations.forked_from_*（6.9）；上一版 9c1f2a7b4d6e


def test_alembic_revisions_parse():
    """真实 alembic/versions：单 head、单基线，head = pinned revision。"""
    assert database._alembic_head_revision() == HEAD
    assert database._alembic_baseline_revision() == BASELINE


def _version_rows(engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
    return [r[0] for r in rows]


def test_legacy_unmanaged_stamps_baseline_then_upgrades(monkeypatch):
    """旧库：补列成功 → 标记基线 → 调 upgrade（升级本身另行验证/注入）。"""
    calls = {"migrate": 0, "upgrade": 0}
    monkeypatch.setattr(
        database,
        "_migrate",
        lambda engine: calls.__setitem__("migrate", calls["migrate"] + 1) or [],
    )
    monkeypatch.setattr(
        database,
        "_upgrade_alembic_head",
        lambda engine: calls.__setitem__("upgrade", calls["upgrade"] + 1),
    )
    engine = create_engine("sqlite://")
    database._legacy_migrate_if_unmanaged(engine)
    assert calls["migrate"] == 1
    assert calls["upgrade"] == 1
    # 旧库结构停在基线年代 → 标记基线（head 由 upgrade 跑上去）
    assert _version_rows(engine) == [BASELINE]


def test_legacy_migrate_failure_blocks_stamp(monkeypatch):
    """补列失败：不写 alembic 标记（下次启动重试）。"""
    monkeypatch.setattr(database, "_migrate", lambda engine: ["conversations.summary"])
    engine = create_engine("sqlite://")
    database._legacy_migrate_if_unmanaged(engine)
    assert not sa_inspect(engine).has_table("alembic_version")


def test_upgrade_failure_clears_stamp_for_retry(monkeypatch):
    """收编后 upgrade 失败：清掉标记回到未纳管态（避免半收编被跳过）。"""
    monkeypatch.setattr(database, "_migrate", lambda engine: [])
    monkeypatch.setattr(
        database,
        "_upgrade_alembic_head",
        lambda engine: (_ for _ in ()).throw(RuntimeError("alembic 环境异常")),
    )
    engine = create_engine("sqlite://")
    database._legacy_migrate_if_unmanaged(engine)
    assert not sa_inspect(engine).has_table("alembic_version")


def test_managed_engine_skips_migrate_but_upgrades(monkeypatch):
    """已纳管库：跳过 _migrate；仍补跑 pending revisions（stamp 在旧版本自动追平）。"""
    calls = {"migrate": 0, "upgrade": 0}

    def _fake_migrate(engine):
        calls["migrate"] += 1
        return []

    def _fake_upgrade(engine):
        calls["upgrade"] += 1

    monkeypatch.setattr(database, "_migrate", _fake_migrate)
    monkeypatch.setattr(database, "_upgrade_alembic_head", _fake_upgrade)
    engine = create_engine("sqlite://")
    database._stamp_alembic_version(engine, BASELINE)  # 模拟库被 stamp 在基线
    database._legacy_migrate_if_unmanaged(engine)
    assert calls["migrate"] == 0
    assert calls["upgrade"] == 1


def test_legacy_file_sqlite_real_upgrade_adds_pinned(monkeypatch, tmp_path):
    """集成：文件 SQLite 旧库 → stamp 基线 → 真 alembic upgrade head。

    env.py 经 ALEMBIC_DATABASE_URL 指向同一个文件库；基线 revision 不会
    重复建表（stamp 跳过了它），只应用其后 pending 的 pinned 列。
    """
    monkeypatch.setattr(database, "_migrate", lambda engine: [])
    db_file = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db_file}")
    # 模拟旧库：有 conversations 但没有 pinned 列、无 alembic_version
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE conversations ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "title VARCHAR(200) NOT NULL, "
                "updated_at DATETIME NOT NULL)"
            )
        )
    database._legacy_migrate_if_unmanaged(engine)

    assert _version_rows(engine) == [HEAD]
    columns = {c["name"] for c in sa_inspect(engine).get_columns("conversations")}
    assert "pinned" in columns
    # 6.9：fork 来源列也由 revision 补齐（旧库收编路径不能漏）
    assert {"forked_from_conversation_id", "forked_from_message_id"} <= columns
    # 已收编：再次调用走管理路径（不重复补列）
    calls = {"migrate": 0}
    monkeypatch.setattr(database, "_migrate", lambda engine: calls.__setitem__("migrate", calls["migrate"] + 1) or [])
    database._legacy_migrate_if_unmanaged(engine)
    assert calls["migrate"] == 0


def test_stamp_head_is_idempotent():
    engine = create_engine("sqlite://")
    assert database._stamp_alembic_head(engine) == HEAD
    assert database._stamp_alembic_head(engine) == HEAD
    assert len(_version_rows(engine)) == 1


def test_upgrade_alembic_head_preserves_password(monkeypatch):
    """注入的 URL 必须带完整密码：SQLAlchemy 2.x 的 str(url) 会渲染成 ***，
    重新解析后连库必然 1045（曾让启动自动迁移静默失败、MySQL 缺列）。"""
    import os

    from alembic import command
    from sqlalchemy.engine import make_url

    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    seen: dict[str, str | None] = {}
    monkeypatch.setattr(
        command,
        "upgrade",
        lambda cfg, rev: seen.update(url=os.environ.get("ALEMBIC_DATABASE_URL")),
    )
    engine = create_engine(
        "mysql+pymysql://root:p%40ss@127.0.0.1:3306/rag_assistant?charset=utf8mb4"
    )
    database._upgrade_alembic_head(engine)
    assert seen["url"] == engine.url.render_as_string(hide_password=False)
    assert make_url(seen["url"]).password == "p@ss"
    assert os.environ.get("ALEMBIC_DATABASE_URL") is None
