"""数据库引擎与会话管理（SQLAlchemy 2.0 + PyMySQL）。

设计说明：
- 启动时做一次"尽力连接"：成功则建表并写入种子提示词模板；
- 连接失败不阻塞服务启动，对话仍可用（仅记忆/模板功能降级），
  日志中会给出明确的错误提示，方便排查 MYSQL_URL 配置。
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import create_engine, text
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
                "MySQL 初始化失败，会话记忆/模板功能不可用。请检查 MYSQL_URL 配置。错误：%s",
                exc2,
            )
            return False


def _activate_engine(new_engine, settings) -> None:
    """连接成功后：建表、准备会话工厂、写入种子模板。"""
    global engine, SessionLocal, db_ready
    engine = new_engine
    # 必须先把模型导入进来，create_all 才会创建对应表
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)
    _migrate(engine)  # 兼容已存在的旧表：补充新增列
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db_ready = True

    # 写入内置提示词模板（幂等）
    from .repository import seed_templates

    with SessionLocal() as db:
        seed_templates(db)
    logger.info("MySQL 初始化完成：%s", settings.mysql_url.split("@")[-1])


def _migrate(engine) -> None:
    """对已存在的表做增量 DDL（SQLAlchemy create_all 不会自动加列）。"""
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
                conn.execute(text("ALTER TABLE conversations ADD COLUMN summary TEXT NULL"))
                logger.info("已为 conversations 添加 summary 列")
            if "summary_up_to_id" not in columns:
                conn.execute(
                    text(
                        "ALTER TABLE conversations "
                        "ADD COLUMN summary_up_to_id BIGINT NOT NULL DEFAULT 0"
                    )
                )
                logger.info("已为 conversations 添加 summary_up_to_id 列")
            if "template_id" not in columns:
                conn.execute(
                    text("ALTER TABLE conversations ADD COLUMN template_id BIGINT NULL")
                )
                logger.info("已为 conversations 添加 template_id 列")

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
                conn.execute(
                    text(
                        "ALTER TABLE memories "
                        "ADD COLUMN category VARCHAR(50) NOT NULL DEFAULT 'other'"
                    )
                )
                logger.info("已为 memories 添加 category 列")
            if "status" not in memory_columns:
                conn.execute(
                    text(
                        "ALTER TABLE memories "
                        "ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'active'"
                    )
                )
                logger.info("已为 memories 添加 status 列")
    except Exception as exc:
        logger.warning("数据库增量迁移失败（不影响主流程）：%s", exc)


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
