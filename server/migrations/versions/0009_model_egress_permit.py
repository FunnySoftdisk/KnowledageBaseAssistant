"""model egress permit

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-11

One row per ModelCall is the PG authority for the final provider hop. Admission and Broker
authorization consume the row through READY -> ADMITTED -> SECRET_READ under row locks.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_egress_permit",
        sa.Column("model_call_id", sa.UUID(), nullable=False),
        sa.Column("route_ref", sa.String(length=256), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_model", sa.String(length=192), nullable=False),
        sa.Column("secret_ref", sa.String(length=256), nullable=False),
        sa.Column("secret_version", sa.Integer(), nullable=False),
        sa.Column("model_revision", sa.String(length=192), nullable=False),
        sa.Column("parameters_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("schema_digest", sa.CHAR(length=64), nullable=True),
        sa.Column("input_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("authorization_version", sa.BigInteger(), nullable=False),
        sa.Column("execution_generation", sa.BigInteger(), nullable=False),
        sa.Column("cancellation_generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("secret_read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "authorization_version >= 1",
            name=op.f("ck_model_egress_permit_authorization_version_positive"),
        ),
        sa.CheckConstraint(
            "cancellation_generation >= 0",
            name=op.f("ck_model_egress_permit_cancellation_generation_nonnegative"),
        ),
        sa.CheckConstraint(
            "parameters_digest ~ '^[0-9a-f]{64}$' AND "
            "(schema_digest IS NULL OR schema_digest ~ '^[0-9a-f]{64}$') AND "
            "input_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_model_egress_permit_digests_lower_hex"),
        ),
        sa.CheckConstraint(
            "execution_generation >= 1",
            name=op.f("ck_model_egress_permit_execution_generation_positive"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_model_egress_permit_expiry_after_creation"),
        ),
        sa.CheckConstraint(
            "(status = 'READY' AND admitted_at IS NULL AND secret_read_at IS NULL) OR "
            "(status = 'ADMITTED' AND admitted_at IS NOT NULL AND secret_read_at IS NULL) OR "
            "(status = 'SECRET_READ' AND admitted_at IS NOT NULL AND secret_read_at IS NOT NULL) OR "
            "(status IN ('REVOKED','EXPIRED') AND secret_read_at IS NULL)",
            name=op.f("ck_model_egress_permit_lifecycle_timestamps"),
        ),
        sa.CheckConstraint(
            "provider = 'alibaba_bailian'",
            name=op.f("ck_model_egress_permit_provider_allowed"),
        ),
        sa.CheckConstraint(
            "row_version >= 1",
            name=op.f("ck_model_egress_permit_row_version_positive"),
        ),
        sa.CheckConstraint(
            "secret_version >= 1",
            name=op.f("ck_model_egress_permit_secret_version_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('READY','ADMITTED','SECRET_READ','REVOKED','EXPIRED')",
            name=op.f("ck_model_egress_permit_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["model_call_id"],
            ["workflow.model_call.id"],
            name=op.f("fk_model_egress_permit_model_call_id_model_call"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("model_call_id", name=op.f("pk_model_egress_permit")),
        schema="workflow",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM workflow.model_egress_permit) THEN "
            "RAISE EXCEPTION 'MODEL_EGRESS_PERMIT_DOWNGRADE_REQUIRES_RECONCILIATION'; "
            "END IF; END $$"
        )
    )
    op.drop_table("model_egress_permit", schema="workflow")
