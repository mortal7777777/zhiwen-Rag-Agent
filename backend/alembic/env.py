"""Alembic 迁移环境。

数据库地址优先级：ALEMBIC_DATABASE_URL > MYSQL_URL > 本地 SQLite（仅用于
生成基线/CI 空库校验）。MYSQL_URL 未设置时自动加载 backend/.env.local（语义
同 run.py：不覆盖已存在的环境变量）——否则命令行 upgrade 会静默落到 SQLite，
MySQL 永远升不上去。不在 ini 里回填 URL，避免密码中的 % 触发 configparser
插值错误。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

from app.db.database import Base  # noqa: E402
import app.db.models  # noqa: F401,E402

target_metadata = Base.metadata


def _load_local_env() -> None:
    """从 backend/.env.local 加载本机配置（不覆盖已存在的环境变量，同 run.py）。"""
    env_file = BACKEND / ".env.local"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_local_env()


def _database_url() -> str:
    return (
        os.environ.get("ALEMBIC_DATABASE_URL")
        or os.environ.get("MYSQL_URL")
        or "sqlite:///./alembic_dev.db"
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
