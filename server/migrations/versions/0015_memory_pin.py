"""memory record pin flag

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-15

DEV-08 Pin 召回通道：`memory.memory_record` 加 `pinned` 布尔标志（§6 用户控制 Pin/Unpin、
§9.2 召回通道2「被用户 Pin 的 Active Memory」）。默认 false；存量 dev 行无需 backfill。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_record",
        sa.Column(
            "pinned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema="memory",
    )


def downgrade() -> None:
    op.drop_column("memory_record", "pinned", schema="memory")
