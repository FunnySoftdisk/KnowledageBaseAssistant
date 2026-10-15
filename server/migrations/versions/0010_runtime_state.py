"""task runtime state

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-14

One row per IntelligentTask is the task-scope authority for the runtime loop: generation fencing,
work version, blocking/ready references, no-progress counters and the loop-level convergence action.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_runtime_state",
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("work_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "execution_generation", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "cancellation_generation", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("next_reconcile_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "blocking_effect_refs_json",
            JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("blocking_issue_json", JSONB(), nullable=True),
        sa.Column("ready_set_ref_json", JSONB(), nullable=True),
        sa.Column("current_loop_fingerprint", sa.CHAR(length=64), nullable=True),
        sa.Column(
            "same_loop_no_progress", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "consecutive_no_progress", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("epoch_no_progress", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("task_no_progress", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "episode_replan_used", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("task_replan_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "episode_seen_loop_fingerprints_json",
            JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("convergence_action", sa.String(length=16), nullable=True),
        sa.Column(
            "replan_required", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("row_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("work_version >= 1", name=op.f("ck_task_runtime_state_work_version_positive")),
        sa.CheckConstraint(
            "execution_generation >= 0",
            name=op.f("ck_task_runtime_state_execution_generation_nonnegative"),
        ),
        sa.CheckConstraint(
            "cancellation_generation >= 0",
            name=op.f("ck_task_runtime_state_cancellation_generation_nonnegative"),
        ),
        sa.CheckConstraint(
            "same_loop_no_progress >= 0",
            name=op.f("ck_task_runtime_state_same_loop_no_progress_nonnegative"),
        ),
        sa.CheckConstraint(
            "consecutive_no_progress >= 0",
            name=op.f("ck_task_runtime_state_consecutive_no_progress_nonnegative"),
        ),
        sa.CheckConstraint(
            "epoch_no_progress >= 0",
            name=op.f("ck_task_runtime_state_epoch_no_progress_nonnegative"),
        ),
        sa.CheckConstraint(
            "task_no_progress >= 0",
            name=op.f("ck_task_runtime_state_task_no_progress_nonnegative"),
        ),
        sa.CheckConstraint(
            "task_replan_count >= 0",
            name=op.f("ck_task_runtime_state_task_replan_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "row_version >= 1", name=op.f("ck_task_runtime_state_row_version_positive")
        ),
        sa.CheckConstraint(
            "convergence_action IS NULL OR convergence_action IN "
            "('CONTINUE_LOOP','REPLAN','CONVERGE')",
            name=op.f("ck_task_runtime_state_convergence_action_allowed"),
        ),
        sa.CheckConstraint(
            "current_loop_fingerprint IS NULL OR current_loop_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_task_runtime_state_loop_fingerprint_lower_hex"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["workflow.intelligent_task.id"],
            name=op.f("fk_task_runtime_state_task_id_intelligent_task"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("task_id", name=op.f("pk_task_runtime_state")),
        schema="workflow",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM workflow.task_runtime_state) THEN "
            "RAISE EXCEPTION 'TASK_RUNTIME_STATE_DOWNGRADE_REQUIRES_RECONCILIATION'; "
            "END IF; END $$"
        )
    )
    op.drop_table("task_runtime_state", schema="workflow")
