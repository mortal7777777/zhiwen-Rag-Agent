"""数据库引擎与会话管理（SQLAlchemy 2.0 + PyMySQL）。

设计说明：
- 启动时做一次"尽力连接"：成功则建表并写入种子提示词模板；
- 连接失败不阻塞服务启动，对话仍可用（仅记忆/模板功能降级），
  日志中会给出明确的错误提示，方便排查 MYSQL_URL 配置。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import declarative_base, sessionmaker

from ..config import get_settings

logger = logging.getLogger(__name__)

Base = declarative_base()

engine = None
SessionLocal: sessionmaker | None = None
db_ready = False


def init_db() -> bool:
    """初始化数据库：连接、建表、写入种子模板。失败时返回 False。"""
    global engine, SessionLocal, db_ready
    settings = get_settings()
    engine_options = dict(
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=5,
        max_overflow=10,
    )
    try:
        engine = create_engine(settings.mysql_url, **engine_options)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        _activate_engine(engine, settings)
        return True
    except Exception as exc:
        # 常见原因：数据库还不存在 -> 尝试自动创建
        try:
            url = make_url(settings.mysql_url)
            db_name = url.database
            if not db_name:
                raise ValueError("MYSQL_URL 未指定数据库名")
            if not db_name.replace("_", "").isalnum():
                raise ValueError(f"数据库名不合法：{db_name}")
            # 注意：url.set(database=None) 在该版本下不会真正去掉库名，
            # 必须用 URL.create 显式重建"无库名"的服务级连接串
            server_url = URL.create(
                drivername=url.drivername,
                username=url.username,
                password=url.password,
                host=url.host,
                port=url.port,
                database=None,
                query=url.query,
            )
            with create_engine(server_url).connect() as conn:
                conn.execute(
                    text(
                        f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
                )
                conn.commit()
            engine = create_engine(settings.mysql_url, **engine_options)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            _activate_engine(engine, settings)
            logger.info("已自动创建数据库 %s 并初始化完成", db_name)
            return True
        except Exception as exc2:
            db_ready = False
            logger.warning(
                "MySQL 初始化失败，会话记忆/模板功能不可用。原因：%s。"
                "若为 Access denied，请确认 MYSQL_URL 使用了正确密码；"
                "可直接运行 start_backend.ps1，或在 backend/.env.local 中"
                "写入 MYSQL_URL=mysql+pymysql://root:你的密码@127.0.0.1:3306/rag_assistant?charset=utf8mb4",
                exc2,
            )
            return False


def _activate_engine(new_engine, settings) -> None:
    """连接成功后：建表、准备会话工厂、写入种子模板。"""
    global engine, SessionLocal, db_ready
    engine = new_engine
    # 必须先把模型导入进来，create_all 才会创建对应表
    from . import models  # noqa: F401

    # 全新库判定要在 create_all 之前：create_all 之后新旧库都是"全部表都在"。
    # 全新库 = create_all 按当前模型建出全部列（结构 == head），直接标记 head；
    # 旧库 = 表结构停留在 _migrate 认知的年代，走 _legacy_migrate_if_unmanaged
    # （stamp 基线 + 补跑 pending revisions）。
    try:
        legacy_db = sa_inspect(engine).has_table("conversations")
    except Exception as exc:  # noqa: BLE001 - 检测失败按旧库路径处理（更保守）
        logger.warning("检测既有表失败（按旧库处理）：%s", exc)
        legacy_db = True

    Base.metadata.create_all(engine)
    if legacy_db:
        _legacy_migrate_if_unmanaged(engine)
    else:
        try:
            _stamp_alembic_head(engine)
        except Exception as exc:  # noqa: BLE001 - 标记失败不阻断启动
            logger.warning("全新库写入 Alembic 标记失败：%s", exc)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db_ready = True

    # 写入内置提示词模板/片段及其版本（幂等）
    from .repository import seed_agent_types, seed_prompt_fragments, seed_templates

    with SessionLocal() as db:
        seed_templates(db)
        try:
            seed_prompt_fragments(db)
        except Exception as exc:
            logger.warning("提示词片段初始化失败（不影响主流程）：%s", exc)
        try:
            seed_agent_types(db)
            from ..agent.agent_types import refresh_registry

            refresh_registry(db)
        except Exception as exc:
            logger.warning("子代理类型初始化失败（使用代码默认）：%s", exc)
    logger.info("MySQL 初始化完成：%s", settings.mysql_url.split("@")[-1])


def _migrate(engine) -> list[str]:
    """对已存在的旧表做增量 DDL（SQLAlchemy create_all 不会自动加列）。

    历史兼容路径：只服务"从未被 Alembic 管理的旧库"（见
    _legacy_migrate_if_unmanaged）。逐条 try：单条 DDL 失败只记录并以
    ERROR 汇总，不阻断启动，但保证"迁移失败没人知道"不会再发生。
    返回失败的标签列表（空 = 全部成功）。
    """
    failures: list[str] = []

    def _ddl(conn, label: str, sql: str) -> None:
        try:
            conn.execute(text(sql))
            logger.info("已%s", label)
        except Exception as exc:
            failures.append(label)
            logger.error("增量迁移失败（%s）：%s。该列/索引可能缺失，请检查权限或手动执行。", label, exc)

    try:
        with engine.begin() as conn:
            columns = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'conversations'"
                    )
                )
            }
            if "summary" not in columns:
                _ddl(conn, "为 conversations 添加 summary 列",
                     "ALTER TABLE conversations ADD COLUMN summary TEXT NULL")
            if "summary_up_to_id" not in columns:
                _ddl(conn, "为 conversations 添加 summary_up_to_id 列",
                     "ALTER TABLE conversations "
                     "ADD COLUMN summary_up_to_id BIGINT NOT NULL DEFAULT 0")
            if "template_id" not in columns:
                _ddl(conn, "为 conversations 添加 template_id 列",
                     "ALTER TABLE conversations ADD COLUMN template_id BIGINT NULL")
            if "prompt_bundle" not in columns:
                _ddl(conn, "为 conversations 添加 prompt_bundle 列",
                     "ALTER TABLE conversations ADD COLUMN prompt_bundle TEXT NULL")
            if "prompt_bundle_hash" not in columns:
                _ddl(conn, "为 conversations 添加 prompt_bundle_hash 列",
                     "ALTER TABLE conversations ADD COLUMN prompt_bundle_hash CHAR(64) NULL")
            if "prompt_locked_at" not in columns:
                _ddl(conn, "为 conversations 添加 prompt_locked_at 列",
                     "ALTER TABLE conversations ADD COLUMN prompt_locked_at DATETIME NULL")

            template_columns = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'prompt_templates'"
                    )
                )
            }
            if template_columns and "system_key" not in template_columns:
                _ddl(conn, "为 prompt_templates 添加 system_key 列",
                     "ALTER TABLE prompt_templates ADD COLUMN system_key VARCHAR(100) NULL")
            if template_columns and "archived" not in template_columns:
                _ddl(conn, "为 prompt_templates 添加 archived 列",
                     "ALTER TABLE prompt_templates ADD COLUMN archived "
                     "TINYINT(1) NOT NULL DEFAULT 0")
            if template_columns and "draft_content" not in template_columns:
                _ddl(conn, "为 prompt_templates 添加 draft_content 列",
                     "ALTER TABLE prompt_templates ADD COLUMN draft_content TEXT NULL")
            if template_columns and "draft_note" not in template_columns:
                _ddl(conn, "为 prompt_templates 添加 draft_note 列",
                     "ALTER TABLE prompt_templates ADD COLUMN draft_note TEXT NULL")
            if template_columns and "draft_updated_at" not in template_columns:
                _ddl(conn, "为 prompt_templates 添加 draft_updated_at 列",
                     "ALTER TABLE prompt_templates ADD COLUMN draft_updated_at DATETIME NULL")
            fragment_columns = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'prompt_fragments'"
                    )
                )
            }
            if fragment_columns and "base_default_hash" not in fragment_columns:
                _ddl(conn, "为 prompt_fragments 添加 base_default_hash 列",
                     "ALTER TABLE prompt_fragments ADD COLUMN base_default_hash "
                     "CHAR(64) NULL")
            if fragment_columns and "draft_content" not in fragment_columns:
                _ddl(conn, "为 prompt_fragments 添加 draft_content 列",
                     "ALTER TABLE prompt_fragments ADD COLUMN draft_content TEXT NULL")
            if fragment_columns and "draft_note" not in fragment_columns:
                _ddl(conn, "为 prompt_fragments 添加 draft_note 列",
                     "ALTER TABLE prompt_fragments ADD COLUMN draft_note TEXT NULL")
            if fragment_columns and "draft_updated_at" not in fragment_columns:
                _ddl(conn, "为 prompt_fragments 添加 draft_updated_at 列",
                     "ALTER TABLE prompt_fragments ADD COLUMN draft_updated_at DATETIME NULL")
            if template_columns:
                indexes = {
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT INDEX_NAME FROM information_schema.STATISTICS "
                            "WHERE TABLE_SCHEMA = DATABASE() "
                            "AND TABLE_NAME = 'prompt_templates'"
                        )
                    )
                }
                if "uq_prompt_templates_system_key" not in indexes:
                    _ddl(
                        conn,
                        "为 prompt_templates 添加 system_key 唯一索引",
                        "CREATE UNIQUE INDEX uq_prompt_templates_system_key "
                        "ON prompt_templates (system_key)",
                    )

            memory_columns = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'memories'"
                    )
                )
            }
            if "category" not in memory_columns:
                _ddl(conn, "为 memories 添加 category 列",
                     "ALTER TABLE memories "
                     "ADD COLUMN category VARCHAR(50) NOT NULL DEFAULT 'other'")
            if "status" not in memory_columns:
                _ddl(conn, "为 memories 添加 status 列",
                     "ALTER TABLE memories "
                     "ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'active'")
            if "hit_count" not in memory_columns:
                _ddl(conn, "为 memories 添加 hit_count 列",
                     "ALTER TABLE memories ADD COLUMN hit_count INT NOT NULL DEFAULT 0")
            if "last_hit_at" not in memory_columns:
                _ddl(conn, "为 memories 添加 last_hit_at 列",
                     "ALTER TABLE memories ADD COLUMN last_hit_at DATETIME NULL")
        if failures:
            logger.error(
                "数据库增量迁移有 %d 条失败：%s。相关功能可能在读取缺失列时报错，"
                "请按日志逐条修复（新变更请改用 alembic revision）。",
                len(failures),
                "；".join(failures),
            )
        return failures
    except Exception as exc:
        logger.warning("数据库增量迁移失败（不影响主流程）：%s", exc)
        return failures or ["<整体迁移失败>"]


def _alembic_versions_dir() -> Path:
    # database.py 位于 backend/app/db/ → backend/alembic/versions
    return Path(__file__).resolve().parent.parent.parent / "alembic" / "versions"


def _parse_alembic_revisions() -> dict[str, str | None]:
    """解析 alembic/versions 下全部 revision（revision id → down_revision）。"""
    revisions: dict[str, str | None] = {}
    for py in sorted(_alembic_versions_dir().glob("*.py")):
        try:
            source = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rev = re.search(
            r"^revision(?::\s*str)?\s*=\s*['\"]([^'\"]+)['\"]", source, re.M
        )
        if not rev:
            continue
        down = re.search(
            r"^down_revision[^=]*=\s*['\"]([^'\"]+)['\"]", source, re.M
        )
        revisions[rev.group(1)] = down.group(1) if down else None
    return revisions


def _alembic_head_revision() -> str:
    """解析 head 版本（无子 revision 者即 head）。不起 alembic 运行时。"""
    revisions = _parse_alembic_revisions()
    children = {down for down in revisions.values() if down}
    heads = [rev for rev in revisions if rev not in children]
    if len(heads) != 1:
        raise RuntimeError(
            f"alembic versions 解析异常（期望单 head，实际 {len(heads)} 个）：{heads}"
        )
    return heads[0]


def _alembic_baseline_revision() -> str:
    """基线版本（down_revision 为 None 者），对应 _migrate 时代的旧库结构。"""
    revisions = _parse_alembic_revisions()
    baselines = [rev for rev, down in revisions.items() if down is None]
    if len(baselines) != 1:
        raise RuntimeError(
            f"alembic versions 解析异常（期望单基线，实际 {len(baselines)} 个）：{baselines}"
        )
    return baselines[0]


def _stamp_alembic_version(engine, target: str) -> None:
    """写入 alembic_version 标记 = target（幂等：清空后插入）。"""
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version ("
                "version_num VARCHAR(32) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            )
        )
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
            {"v": target},
        )


def _stamp_alembic_head(engine) -> str:
    """全新库专用：结构 == 当前模型 == head，直接标记 head。"""
    head = _alembic_head_revision()
    _stamp_alembic_version(engine, head)
    return head


def _drop_alembic_version(engine) -> None:
    """升级失败时清掉标记：让库回到「未纳管」状态，下次启动自动重试。"""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


def _upgrade_alembic_head(engine) -> None:
    """跑 alembic upgrade head（pending revisions 自动补列）。

    env.py 的 URL 优先级是 ALEMBIC_DATABASE_URL > MYSQL_URL > sqlite 兜底，
    所以临时注入本进程 engine 的 URL（跑完恢复；密码只在本进程内流转）。
    """
    import os

    from alembic import command
    from alembic.config import Config

    cfg = Config()  # 不读 alembic.ini：env.py 的 fileConfig 分支自然跳过
    cfg.set_main_option(
        "script_location",
        str(_alembic_versions_dir().parent),
    )
    saved = os.environ.get("ALEMBIC_DATABASE_URL")
    # SQLAlchemy 2.x 的 str(url) 会把密码渲染成 ***，重新解析后必然 1045；
    # 必须走 render_as_string(hide_password=False) 才能原样回喂。
    os.environ["ALEMBIC_DATABASE_URL"] = engine.url.render_as_string(
        hide_password=False
    )
    try:
        command.upgrade(cfg, "head")
    finally:
        if saved is None:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
        else:
            os.environ["ALEMBIC_DATABASE_URL"] = saved


def _legacy_migrate_if_unmanaged(engine) -> None:
    """旧库一次性收编进 Alembic（2026-11-19 双轨收敛）。

    - 无 alembic_version 表：_migrate 补列 → **全部成功才** stamp 基线
      （基线 = 旧库实际所处的版本）→ upgrade head（基线之后的新列由
      revision 提供）。upgrade 失败会清掉标记回到未纳管态，下次启动重试，
      避免"标了基线却没升上去"的半收编状态；
    - 已有 alembic_version 表：跳过 _migrate（结构变更只走 revision），
      并补跑 pending revisions（stamp 在旧版本上的库自动追平 head）。
    """
    try:
        if sa_inspect(engine).has_table("alembic_version"):
            logger.debug("数据库已由 Alembic 管理，跳过旧库补列（_migrate）")
            try:
                _upgrade_alembic_head(engine)
            except Exception as exc:  # noqa: BLE001 - 升级失败不阻断启动
                logger.error(
                    "Alembic 升级失败（启动继续，可能缺列报错）：%s。"
                    "请修复后重启或手动执行 python -m alembic upgrade head。",
                    exc,
                )
            return
    except Exception as exc:
        logger.warning("检测 alembic_version 失败（跳过收编）：%s", exc)
        return
    failures = _migrate(engine)
    if failures:
        logger.error(
            "旧库补列有 %d 条失败，暂不写入 Alembic 标记，修复后下次启动自动重试",
            len(failures),
        )
        return
    baseline = _alembic_baseline_revision()
    head = _alembic_head_revision()
    _stamp_alembic_version(engine, baseline)
    if head != baseline:
        try:
            _upgrade_alembic_head(engine)
            logger.info("旧库已收编进 Alembic（stamp 基线 %s → upgrade head）", baseline)
        except Exception as exc:  # noqa: BLE001
            # 回滚到未纳管：保留半收编状态只会让下次启动跳过补列
            logger.exception("收编后升级失败，已清除 Alembic 标记待下次重试：%s", exc)
            _drop_alembic_version(engine)
            return
    else:
        logger.info("旧库已收编进 Alembic（stamp → %s）", baseline)


def get_db():
    """FastAPI 依赖：每个请求一个会话，用完即关。"""
    if not db_ready or SessionLocal is None:
        raise HTTPException(
            status_code=503,
            detail="数据库未连接：请检查 MYSQL_URL 配置后重启服务",
        )
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_optional():
    """软依赖：DB 未连接时 yield None（普通上传不应因 MySQL 宕机而失效；
    只有"同名归档 / 版本管理"这类必须落库的分支再显式报错）。"""
    if not db_ready or SessionLocal is None:
        yield None
        return
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
