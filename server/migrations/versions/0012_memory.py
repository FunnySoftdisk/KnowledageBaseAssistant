"""memory schema: logical record, provenance, forget tombstone

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-15

DEV-08 写侧权威 Store 迁移包（ADR-0004§4）：独立 `memory` Schema 的不可变 Revision
逻辑记录表 + 遗忘 Tombstone。Provenance 以 JSONB 内联（沿用 `workflow` 惯例）；Embedding
Projection（pgvector）、Conflict Group 独立表与 Outbox 留待召回/embedding 切片。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE_VALUES = "'TASK','CONVERSATION','USER_PRIVATE'"
_KIND_VALUES = (
    "'GOAL','CONSTRAINT','DEFINITION','DECISION','PREFERENCE','ENTITY_BINDING',"
    "'OPEN_QUESTION','RESOLUTION','SUPPORTED_FINDING'"
)
_STATE_VALUES = (
    "'PROPOSED','ACTIVE','SUPERSEDED','RETRACTED','EXPIRED','INVALIDATED','QUARANTINED'"
)
_TRUST_VALUES = "'USER_EXPLICIT','USER_DERIVED','ASSISTANT_DERIVED','EVIDENCE_SUPPORTED'"


def upgrade() -> None:
    op.execute(sa.text('CREATE SCHEMA IF NOT EXISTS "memory"'))

    op.create_table(
        "memory_record",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("task_id", sa.UUID(), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("trust", sa.String(length=32), nullable=False),
        sa.Column("canonical_key", sa.String(length=128), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes_id", sa.UUID(), nullable=True),
        sa.Column("conflict_group_id", sa.UUID(), nullable=True),
        sa.Column("provenance_json", JSONB(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"scope IN ({_SCOPE_VALUES})", name=op.f("ck_memory_record_scope_allowed")
        ),
        sa.CheckConstraint(
            f"kind IN ({_KIND_VALUES})", name=op.f("ck_memory_record_kind_allowed")
        ),
        sa.CheckConstraint(
            f"state IN ({_STATE_VALUES})", name=op.f("ck_memory_record_state_allowed")
        ),
        sa.CheckConstraint(
            f"trust IN ({_TRUST_VALUES})", name=op.f("ck_memory_record_trust_allowed")
        ),
        sa.CheckConstraint(
            "importance >= 0 AND importance <= 1", name=op.f("ck_memory_record_importance_range")
        ),
        sa.CheckConstraint(
            "canonical_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_memory_record_canonical_hash_lower_hex"),
        ),
        sa.CheckConstraint(
            "schema_version >= 1", name=op.f("ck_memory_record_schema_version_positive")
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_memory_record_revision_positive")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["iam.organization.id"],
            name=op.f("fk_memory_record_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["iam.user_account.id"],
            name=op.f("fk_memory_record_owner_id_user_account"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["workflow.conversation.id"],
            name=op.f("fk_memory_record_conversation_id_conversation"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["workflow.intelligent_task.id"],
            name=op.f("fk_memory_record_task_id_intelligent_task"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["memory.memory_record.id"],
            name=op.f("fk_memory_record_supersedes_id_memory_record"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_record")),
        schema="memory",
    )
    op.create_index(
        "ix_memory_record_owner_scope_state_hash",
        "memory_record",
        ["owner_id", "scope", "state", "canonical_hash"],
        unique=False,
        schema="memory",
    )
    op.create_index(
        "ix_memory_record_owner_scope_state_key",
        "memory_record",
        ["owner_id", "scope", "state", "canonical_key"],
        unique=False,
        schema="memory",
    )

    op.create_table(
        "memory_forget_tombstone",
        sa.Column("tombstone_id", sa.UUID(), nullable=False),
        sa.Column("record_id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("reason", sa.String(length=256), nullable=False),
        sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(reason) BETWEEN 1 AND 256",
            name=op.f("ck_memory_forget_tombstone_reason_length"),
        ),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["memory.memory_record.id"],
            name=op.f("fk_memory_forget_tombstone_record_id_memory_record"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["iam.organization.id"],
            name=op.f("fk_memory_forget_tombstone_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["iam.user_account.id"],
            name=op.f("fk_memory_forget_tombstone_owner_id_user_account"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tombstone_id", name=op.f("pk_memory_forget_tombstone")),
        schema="memory",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM memory.memory_forget_tombstone) THEN "
            "RAISE EXCEPTION 'MEMORY_FORGET_TOMBSTONE_DOWNGRADE_REQUIRES_RECONCILIATION'; "
            "END IF; END $$"
        )
    )
    op.drop_table("memory_forget_tombstone", schema="memory")
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM memory.memory_record) THEN "
            "RAISE EXCEPTION 'MEMORY_RECORD_DOWNGRADE_REQUIRES_RECONCILIATION'; "
            "END IF; END $$"
        )
    )
    op.drop_table("memory_record", schema="memory")
