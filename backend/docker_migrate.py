"""容器启动前的数据库迁移。

旧数据卷里的表由 create_all/_migrate 建出、没有 alembic_version：
直接 upgrade 会尝试重复建表。检测到"有 conversations 表但没有
alembic_version"时先 stamp head（与 README 的旧库升级步骤一致），
再执行 upgrade head。

数据库地址与 alembic/env.py 同口径：ALEMBIC_DATABASE_URL > MYSQL_URL
> 本地 SQLite。MySQL 未就绪时重试 10 次（每次 3 秒），失败即退出，
由容器编排决定是否重启。
"""

from __future__ import annotations

import os
import sys
import time

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool


def database_url() -> str:
    return (
        os.environ.get("ALEMBIC_DATABASE_URL")
        or os.environ.get("MYSQL_URL")
        or "sqlite:///./alembic_dev.db"
    )


def needs_stamp(engine) -> bool:
    """旧库判据：已有业务表但没有 alembic 版本表。"""
    tables = set(inspect(engine).get_table_names())
    return "alembic_version" not in tables and "conversations" in tables


def wait_for_database(url: str, attempts: int = 10, delay: float = 3.0):
    last_error: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            engine = create_engine(url, poolclass=NullPool)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return engine
        except Exception as exc:  # noqa: BLE001 - 启动重试：错误统一转日志
            last_error = exc
            print(f"[migrate] 数据库未就绪（{i}/{attempts}）：{exc}", flush=True)
            if i < attempts:
                time.sleep(delay)
    raise RuntimeError(f"数据库连接失败：{last_error}")


def main() -> int:
    url = database_url()
    try:
        engine = wait_for_database(url)
        cfg = Config("alembic.ini")
        if needs_stamp(engine):
            print("[migrate] 检测到旧库（无 alembic_version），先 stamp head", flush=True)
            command.stamp(cfg, "head")
        command.upgrade(cfg, "head")
        print("[migrate] 数据库迁移完成", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001 - 入口脚本：失败即阻断启动
        print(f"[migrate] 迁移失败，拒绝启动：{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
