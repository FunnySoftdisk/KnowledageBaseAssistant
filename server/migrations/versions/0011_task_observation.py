"""task observation

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-14

Immutable observation projection and append-only disposition events backing the
Action -> Observation -> Controller loop.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OBSERVATION_SOURCE_KINDS = (
    "'RESULT_ADOPTED','RESULT_NOT_ADOPTED','ACTION_TERMINAL','APPROVAL_TERMINAL',"
    "'EXTERNAL_EFFECT_CHANGED','TASK_INPUT_ADVANCED','RESOURCE_AUTHORIZATION_CHANGED',"
    "'CAPABILITY_AVAILABILITY_CHANGED','BUDGET_THRESHOLD_REACHED','TASK_DEADLINE_REACHED',"
    "'AGENT_ASSESSMENT_REPORTED'"
)


def upgrade() -> None:
    op.create_table(
        "task_observation",
        sa.Column("observation_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("source_event_id", sa.UUID(), nullable=False),
        sa.Column("source_event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("source_ordinal", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column("observation_class", sa.String(length=32), nullable=False),
        sa.Column("source_plan_version", sa.Integer(), nullable=True),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", JSONB(), nullable=False),
        sa.Column("integrity_digest", sa.CHAR(length=64), nullable=False),
        sa.CheckConstraint(
            "contract_version = 'task_observation_v1'",
            name=op.f("ck_task_observation_contract_version_v1"),
        ),
        sa.CheckConstraint(
            "source_event_sequence >= 1",
            name=op.f("ck_task_observation_source_event_sequence_positive"),
        ),
        sa.CheckConstraint(
            "source_ordinal >= 0", name=op.f("ck_task_observation_source_ordinal_nonnegative")
        ),
        sa.CheckConstraint(
            "source_plan_version IS NULL OR source_plan_version >= 1",
            name=op.f("ck_task_observation_source_plan_version_positive"),
        ),
        sa.CheckConstraint(
            "integrity_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_task_observation_integrity_digest_lower_hex"),
        ),
        sa.CheckConstraint(
            f"source_kind IN ({_OBSERVATION_SOURCE_KINDS})",
            name=op.f("ck_task_observation_source_kind_allowed"),
        ),
        sa.CheckConstraint(
            "observation_class IN ('ADOPTED_RESULT','SYSTEM_FACT','USER_INPUT','AGENT_ASSESSMENT')",
            name=op.f("ck_task_observation_observation_class_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["workflow.intelligent_task.id"],
            name=op.f("fk_task_observation_task_id_intelligent_task"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("observation_id", name=op.f("pk_task_observation")),
        schema="workflow",
    )

    op.create_table(
        "observation_disposition",
        sa.Column("disposition_event_id", sa.UUID(), nullable=False),
        sa.Column("observation_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("disposition", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=False),
        sa.Column("caused_by_event_id", sa.UUID(), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "contract_version = 'observation_disposition_event_v1'",
            name=op.f("ck_observation_disposition_contract_version_v1"),
        ),
        sa.CheckConstraint(
            "disposition IN ('RESOLVED','SUPERSEDED','INVALIDATED')",
            name=op.f("ck_observation_disposition_disposition_allowed"),
        ),
        sa.CheckConstraint(
            "char_length(reason_code) BETWEEN 1 AND 128",
            name=op.f("ck_observation_disposition_reason_code_length"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["workflow.task_observation.observation_id"],
            name=op.f("fk_observation_disposition_observation_id_task_observation"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["workflow.intelligent_task.id"],
            name=op.f("fk_observation_disposition_task_id_intelligent_task"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("disposition_event_id", name=op.f("pk_observation_disposition")),
        schema="workflow",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM workflow.observation_disposition) THEN "
            "RAISE EXCEPTION 'OBSERVATION_DISPOSITION_DOWNGRADE_REQUIRES_RECONCILIATION'; "
            "END IF; END $$"
        )
    )
    op.drop_table("observation_disposition", schema="workflow")
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM workflow.task_observation) THEN "
            "RAISE EXCEPTION 'TASK_OBSERVATION_DOWNGRADE_REQUIRES_RECONCILIATION'; "
            "END IF; END $$"
        )
    )
    op.drop_table("task_observation", schema="workflow")
