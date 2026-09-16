"""goal_clarification_request

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_input_request",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("input_id", sa.UUID(), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("source_task_attempt_id", sa.UUID(), nullable=False),
        sa.Column("question_artifact_id", sa.UUID(), nullable=False),
        sa.Column("input_schema_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("response_contract_version", sa.String(length=64), nullable=False),
        sa.Column("display_summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("response_artifact_id", sa.UUID(), nullable=True),
        sa.Column("response_size_bytes", sa.Integer(), nullable=True),
        sa.Column("goal_understanding_id", sa.UUID(), nullable=True),
        sa.Column("clarification_round_no", sa.Integer(), nullable=True),
        sa.Column("resume_bundle_id", sa.UUID(), nullable=True),
        sa.Column("deferred_call_id", sa.String(length=255), nullable=True),
        sa.Column("runtime_blocking_issue_id", sa.UUID(), nullable=True),
        sa.Column("issue_fingerprint", sa.CHAR(length=64), nullable=True),
        sa.Column("request_version", sa.Integer(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "origin IN ('GOAL_CLARIFICATION','DEFERRED_TOOL','RUNTIME_RESOLUTION')",
            name=op.f("ck_user_input_request_origin_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','SUBMITTED','EXPIRED','CANCELLED')",
            name=op.f("ck_user_input_request_status_allowed"),
        ),
        sa.CheckConstraint(
            "request_version >= 1",
            name=op.f("ck_user_input_request_request_version_positive"),
        ),
        sa.CheckConstraint(
            "row_version >= 1", name=op.f("ck_user_input_request_row_version_positive")
        ),
        sa.CheckConstraint(
            "expires_at > requested_at",
            name=op.f("ck_user_input_request_expiry_after_request"),
        ),
        sa.CheckConstraint(
            "(status = 'SUBMITTED' AND response_artifact_id IS NOT NULL AND "
            "response_size_bytes BETWEEN 1 AND 16384 AND submitted_at IS NOT NULL) OR "
            "(status <> 'SUBMITTED' AND response_artifact_id IS NULL AND "
            "response_size_bytes IS NULL AND submitted_at IS NULL)",
            name=op.f("ck_user_input_request_response_status_shape"),
        ),
        sa.CheckConstraint(
            "(origin = 'GOAL_CLARIFICATION' AND goal_understanding_id IS NOT NULL AND "
            "clarification_round_no BETWEEN 1 AND 2 AND resume_bundle_id IS NULL AND "
            "deferred_call_id IS NULL AND runtime_blocking_issue_id IS NULL AND "
            "issue_fingerprint IS NULL) OR "
            "(origin = 'DEFERRED_TOOL' AND goal_understanding_id IS NULL AND "
            "clarification_round_no IS NULL AND resume_bundle_id IS NOT NULL AND "
            "deferred_call_id IS NOT NULL AND runtime_blocking_issue_id IS NULL AND "
            "issue_fingerprint IS NULL) OR "
            "(origin = 'RUNTIME_RESOLUTION' AND goal_understanding_id IS NULL AND "
            "clarification_round_no IS NULL AND resume_bundle_id IS NULL AND "
            "deferred_call_id IS NULL AND runtime_blocking_issue_id IS NOT NULL AND "
            "issue_fingerprint IS NOT NULL)",
            name=op.f("ck_user_input_request_origin_branch_shape"),
        ),
        sa.CheckConstraint(
            "origin = 'DEFERRED_TOOL' OR "
            "response_contract_version = 'clarification_response_contract_v1'",
            name=op.f("ck_user_input_request_clarification_response_contract"),
        ),
        sa.CheckConstraint(
            "issue_fingerprint IS NULL OR issue_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_user_input_request_issue_fingerprint_lower_hex"),
        ),
        sa.ForeignKeyConstraint(
            ["goal_understanding_id"],
            ["workflow.task_goal_understanding.understanding_id"],
            name=op.f("fk_user_input_request_goal_understanding_id_task_goal_understanding"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["input_id"],
            ["workflow.task_input_snapshot.input_id"],
            name=op.f("fk_user_input_request_input_id_task_input_snapshot"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["question_artifact_id"],
            ["content.artifact.artifact_id"],
            name=op.f("fk_user_input_request_question_artifact_id_artifact"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["response_artifact_id"],
            ["content.artifact.artifact_id"],
            name=op.f("fk_user_input_request_response_artifact_id_artifact"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_task_attempt_id"],
            ["workflow.task_attempt.id"],
            name=op.f("fk_user_input_request_source_task_attempt_id_task_attempt"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["workflow.intelligent_task.id"],
            name=op.f("fk_user_input_request_task_id_intelligent_task"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_input_request")),
        sa.UniqueConstraint(
            "task_id",
            "input_id",
            "clarification_round_no",
            name=op.f("uq_user_input_request_task_id_input_id_clarification_round_no"),
        ),
        schema="workflow",
    )
    op.create_index(
        "uq_user_input_request_deferred_call",
        "user_input_request",
        ["resume_bundle_id", "deferred_call_id"],
        unique=True,
        schema="workflow",
        postgresql_where=sa.text("origin = 'DEFERRED_TOOL'"),
    )
    op.create_index(
        "uq_user_input_request_runtime_issue",
        "user_input_request",
        ["task_id", "issue_fingerprint"],
        unique=True,
        schema="workflow",
        postgresql_where=sa.text("origin = 'RUNTIME_RESOLUTION'"),
    )
    op.create_index(
        "uq_user_input_request_task_pending",
        "user_input_request",
        ["task_id"],
        unique=True,
        schema="workflow",
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    # Request及回答引用是业务事实；有数据时只能先备份/对账，不能以降级静默删除。
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM workflow.user_input_request) THEN "
            "RAISE EXCEPTION 'GOAL_CLARIFICATION_DOWNGRADE_REQUIRES_BACKUP_RESTORE'; "
            "END IF; END $$"
        )
    )
    op.drop_index(
        "uq_user_input_request_task_pending",
        table_name="user_input_request",
        schema="workflow",
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.drop_index(
        "uq_user_input_request_runtime_issue",
        table_name="user_input_request",
        schema="workflow",
        postgresql_where=sa.text("origin = 'RUNTIME_RESOLUTION'"),
    )
    op.drop_index(
        "uq_user_input_request_deferred_call",
        table_name="user_input_request",
        schema="workflow",
        postgresql_where=sa.text("origin = 'DEFERRED_TOOL'"),
    )
    op.drop_table("user_input_request", schema="workflow")
