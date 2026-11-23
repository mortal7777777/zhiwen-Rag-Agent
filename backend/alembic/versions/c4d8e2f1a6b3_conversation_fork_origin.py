"""conversations: fork 来源字段（6.9）

Revision ID: c4d8e2f1a6b3
Revises: 9c1f2a7b4d6e
Create Date: 2026-11-21

会话级 fork 需要记录"从哪个会话的哪条消息分支出来"，供前端展示来源。
启动流程先 create_all 再跑 upgrade：全新库的列已由模型建出，这里先检查
列是否存在再 add（与 9c1f2a7b4d6e 的 has_table 守卫同理）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4d8e2f1a6b3"
down_revision: Union[str, Sequence[str], None] = "9c1f2a7b4d6e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(table):
        return set()
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    existing = _columns("conversations")
    if "forked_from_conversation_id" not in existing:
        op.add_column(
            "conversations",
            sa.Column("forked_from_conversation_id", sa.BigInteger(), nullable=True),
        )
    if "forked_from_message_id" not in existing:
        op.add_column(
            "conversations",
            sa.Column("forked_from_message_id", sa.BigInteger(), nullable=True),
        )


def downgrade() -> None:
    existing = _columns("conversations")
    if "forked_from_message_id" in existing:
        op.drop_column("conversations", "forked_from_message_id")
    if "forked_from_conversation_id" in existing:
        op.drop_column("conversations", "forked_from_conversation_id")
