"""conversations.pinned 置顶列

Revision ID: 3f8c21e0a9d4
Revises: 6018474f2912
Create Date: 2026-11-19

P3·2026-11-19：会话列表置顶功能。server_default=false() 让迁移在存量行上
安全执行（旧行补 0）；模型侧 default=False 覆盖新写入。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3f8c21e0a9d4"
down_revision: Union[str, Sequence[str], None] = "6018474f2912"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add pinned column to conversations."""
    op.add_column(
        "conversations",
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Drop pinned column."""
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("pinned")
