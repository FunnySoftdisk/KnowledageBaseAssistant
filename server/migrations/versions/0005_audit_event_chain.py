"""audit_event_chain

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS audit")
    op.create_table(
        "audit_event",
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("actor_role_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("source_ip", sa.String(length=64), nullable=True),
        sa.Column("device_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("before_digest", sa.CHAR(length=64), nullable=True),
        sa.Column("after_digest", sa.CHAR(length=64), nullable=True),
        sa.Column("details_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("previous_hash", sa.CHAR(length=64), nullable=True),
        sa.Column("event_hash", sa.CHAR(length=64), nullable=False),
        sa.CheckConstraint(
            "result IN ('SUCCESS','FAILURE','DENIED')",
            name=op.f("ck_audit_event_result_allowed"),
        ),
        sa.CheckConstraint(
            "event_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_audit_event_event_hash_lower_hex"),
        ),
        sa.CheckConstraint(
            "previous_hash IS NULL OR previous_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_audit_event_previous_hash_lower_hex"),
        ),
        sa.CheckConstraint(
            "before_digest IS NULL OR before_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_audit_event_before_digest_lower_hex"),
        ),
        sa.CheckConstraint(
            "after_digest IS NULL OR after_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_audit_event_after_digest_lower_hex"),
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_audit_event")),
        schema="audit",
    )
    op.create_index(
        "ix_audit_event_occurred_at", "audit_event", ["occurred_at"], schema="audit"
    )
    op.create_index("ix_audit_event_actor_id", "audit_event", ["actor_id"], schema="audit")
    op.create_index(
        "ix_audit_event_resource",
        "audit_event",
        ["resource_type", "resource_id"],
        schema="audit",
    )

    op.create_table(
        "audit_chain_head",
        sa.Column("chain_key", sa.String(length=64), nullable=False),
        sa.Column("head_hash", sa.CHAR(length=64), nullable=True),
        sa.PrimaryKeyConstraint("chain_key", name=op.f("pk_audit_chain_head")),
        schema="audit",
    )
    op.execute(
        "INSERT INTO audit.audit_chain_head (chain_key, head_hash) VALUES ('default', NULL)"
    )

    op.execute(
        "CREATE FUNCTION audit.reject_audit_mutation() RETURNS trigger AS $$\n"
        "BEGIN\n"
        "    RAISE EXCEPTION 'AUDIT_EVENT_IMMUTABLE' USING ERRCODE = 'check_violation';\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER audit_event_immutable\n"
        "BEFORE UPDATE OR DELETE ON audit.audit_event\n"
        "FOR EACH ROW EXECUTE FUNCTION audit.reject_audit_mutation()"
    )


def downgrade() -> None:
    # 审计是追加事实；有数据时只能先备份/对账，不能以降级静默删除。
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM audit.audit_event) THEN "
            "RAISE EXCEPTION 'AUDIT_DOWNGRADE_REQUIRES_BACKUP_RESTORE'; "
            "END IF; END $$"
        )
    )
    op.execute("DROP TRIGGER IF EXISTS audit_event_immutable ON audit.audit_event")
    op.execute("DROP FUNCTION IF EXISTS audit.reject_audit_mutation()")
    op.drop_table("audit_chain_head", schema="audit")
    op.drop_index("ix_audit_event_resource", table_name="audit_event", schema="audit")
    op.drop_index("ix_audit_event_actor_id", table_name="audit_event", schema="audit")
    op.drop_index("ix_audit_event_occurred_at", table_name="audit_event", schema="audit")
    op.drop_table("audit_event", schema="audit")
    op.execute("DROP SCHEMA IF EXISTS audit")
