"""agent_types 子代理类型注册表

Revision ID: 9c1f2a7b4d6e
Revises: 3f8c21e0a9d4
Create Date: 2026-11-21

Phase A 6.1：把 research/explore/review/execute 从硬编码升级为可配置类型。
种子行由启动时 `seed_agent_types` 幂等写入，迁移只建表。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9c1f2a7b4d6e"
down_revision: Union[str, Sequence[str], None] = "3f8c21e0a9d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 启动流程先 Base.metadata.create_all 再跑 upgrade：全新表已存在时直接
    # 跳过建表（否则 "table already exists" 会让 alembic_version 停在旧 head）。
    bind = op.get_bind()
    if sa.inspect(bind).has_table("agent_types"):
        return
    op.create_table(
        "agent_types",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("mode", sa.String(length=20), nullable=False, server_default="research"),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("tools", sa.Text(), nullable=True),
        sa.Column("max_rounds", sa.Integer(), nullable=True),
        sa.Column("model_provider_id", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column("thinking", sa.String(length=20), nullable=True, server_default="inherit"),
        sa.Column("llm_budget", sa.Integer(), nullable=True),
        sa.Column("read_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_agent_types_key", "agent_types", ["key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_agent_types_key", table_name="agent_types")
    op.drop_table("agent_types")
