"""model_call context pack reference triple

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-15

DEV-08 模型网关接线消费：`workflow.model_call` 补齐 Context Pack 引用三件套中的 Hash 与
Input Digest 两列（`context_pack_id` 已由 0006 建列）。模块10 `GenerationRequest`/§12 要求
ModelCall 记录 `context_pack_id/hash/input_digest`；Hash 即存储头 `pack_hash`、Input Digest
即存储头 `input_digest`（64 位小写 hex）。dev 表空，NOT NULL 无 backfill。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_call",
        sa.Column("context_pack_hash", sa.CHAR(length=64), nullable=False),
        schema="workflow",
    )
    op.add_column(
        "model_call",
        sa.Column("context_pack_input_digest", sa.CHAR(length=64), nullable=False),
        schema="workflow",
    )
    op.create_check_constraint(
        "ck_model_call_context_pack_ref_lower_hex",
        "model_call",
        "context_pack_hash ~ '^[0-9a-f]{64}$' AND "
        "context_pack_input_digest ~ '^[0-9a-f]{64}$'",
        schema="workflow",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_model_call_context_pack_ref_lower_hex",
        "model_call",
        type_="check",
        schema="workflow",
    )
    op.drop_column("model_call", "context_pack_input_digest", schema="workflow")
    op.drop_column("model_call", "context_pack_hash", schema="workflow")
