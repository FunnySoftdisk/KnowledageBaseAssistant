"""reconcile frozen context_pack schema with immutable-pack domain contract

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-15

DEV-08 Context Pack 持久化对账迁移：`workflow.context_pack` / `workflow.context_pack_item`
由 DEV-03 依赖切片提前建表时，把若干列过度约束为 NOT NULL，与模块16 的不可变 Pack 契约
（§4.3/§5.1）不一致。本迁移只放宽可空性，不改列名/列序/外键：

- `context_pack.conversation_id`：任务级 Pack 可无会话（域 `conversation_id: UUID | None`）。
- `context_pack.tokenizer_revision`/`chat_template_hash`：`ESTIMATED_DEV` Token 计数无
  Tokenizer/Chat Template（其身份已由 `input_digest` 内容寻址），仅 `EXACT` 落值。
- `context_pack_item.source_type`/`source_id`/`source_version`/`bucket`：强制项
  （SYSTEM_POLICY/TOOL_SCHEMA/OUTPUT_SCHEMA/CURRENT_MESSAGE）无单一来源、无弹性预算桶。
- `context_pack_item.rank_score`：强制项与近期 Active 项无相关度分（域 `rank_score: float|None`）。

零新增列、零改序；字段矩阵（列名投影）无需变更。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "context_pack",
        "conversation_id",
        existing_type=sa.UUID(),
        nullable=True,
        schema="workflow",
    )
    op.alter_column(
        "context_pack",
        "tokenizer_revision",
        existing_type=sa.String(length=128),
        nullable=True,
        schema="workflow",
    )
    op.alter_column(
        "context_pack",
        "chat_template_hash",
        existing_type=sa.CHAR(length=64),
        nullable=True,
        schema="workflow",
    )
    op.alter_column(
        "context_pack_item",
        "source_type",
        existing_type=sa.String(length=64),
        nullable=True,
        schema="workflow",
    )
    op.alter_column(
        "context_pack_item",
        "source_id",
        existing_type=sa.UUID(),
        nullable=True,
        schema="workflow",
    )
    op.alter_column(
        "context_pack_item",
        "source_version",
        existing_type=sa.String(length=64),
        nullable=True,
        schema="workflow",
    )
    op.alter_column(
        "context_pack_item",
        "rank_score",
        existing_type=sa.Float(),
        nullable=True,
        schema="workflow",
    )
    op.alter_column(
        "context_pack_item",
        "bucket",
        existing_type=sa.String(length=64),
        nullable=True,
        schema="workflow",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS ("
            "SELECT 1 FROM workflow.context_pack "
            "WHERE conversation_id IS NULL OR tokenizer_revision IS NULL "
            "OR chat_template_hash IS NULL"
            ") OR EXISTS ("
            "SELECT 1 FROM workflow.context_pack_item "
            "WHERE source_type IS NULL OR source_id IS NULL OR source_version IS NULL "
            "OR rank_score IS NULL OR bucket IS NULL"
            ") THEN RAISE EXCEPTION 'CONTEXT_PACK_DOWNGRADE_REQUIRES_RECONCILIATION'; "
            "END IF; END $$"
        )
    )
    op.alter_column(
        "context_pack",
        "conversation_id",
        existing_type=sa.UUID(),
        nullable=False,
        schema="workflow",
    )
    op.alter_column(
        "context_pack",
        "tokenizer_revision",
        existing_type=sa.String(length=128),
        nullable=False,
        schema="workflow",
    )
    op.alter_column(
        "context_pack",
        "chat_template_hash",
        existing_type=sa.CHAR(length=64),
        nullable=False,
        schema="workflow",
    )
    op.alter_column(
        "context_pack_item",
        "source_type",
        existing_type=sa.String(length=64),
        nullable=False,
        schema="workflow",
    )
    op.alter_column(
        "context_pack_item",
        "source_id",
        existing_type=sa.UUID(),
        nullable=False,
        schema="workflow",
    )
    op.alter_column(
        "context_pack_item",
        "source_version",
        existing_type=sa.String(length=64),
        nullable=False,
        schema="workflow",
    )
    op.alter_column(
        "context_pack_item",
        "rank_score",
        existing_type=sa.Float(),
        nullable=False,
        schema="workflow",
    )
    op.alter_column(
        "context_pack_item",
        "bucket",
        existing_type=sa.String(length=64),
        nullable=False,
        schema="workflow",
    )
