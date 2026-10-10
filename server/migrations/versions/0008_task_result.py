"""task result

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-09

DEV-06：`workflow.task_result` 终态结果表。与 COMPLETED Task 一对一，`(task_id, result_version)`
复合主键且首版 `result_version=1` 不可覆盖；保存 Answer/Envelope 内容寻址 Artifact、Criterion/
Constraint/Degradation 快照、Final Snapshot/Attempt、Candidate/Validation/Evaluation/Gate
Artifact、Renderer Profile 与 Commit Digest/Receipt。`final_synthesis_snapshot`/
`final_synthesis_attempt`/`task_result_commit_receipt` 尚未冻结，`final_snapshot_id`/
`final_attempt_id`/`commit_receipt_id` 保持裸 UUID 列；`intelligent_task`/`content.artifact`
已冻结，落地真实外键。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_result",
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("contract_version", sa.String(length=32), nullable=False),
        sa.Column("result_version", sa.Integer(), nullable=False),
        sa.Column("answer_status", sa.String(length=32), nullable=False),
        sa.Column("answer_artifact_id", sa.UUID(), nullable=False),
        sa.Column("answer_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("result_envelope_artifact_id", sa.UUID(), nullable=False),
        sa.Column("citation_count", sa.Integer(), nullable=False),
        sa.Column("result_artifact_count", sa.Integer(), nullable=False),
        sa.Column("criterion_checks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("constraint_checks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("degradation_flags", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("final_snapshot_id", sa.UUID(), nullable=False),
        sa.Column("final_attempt_id", sa.UUID(), nullable=False),
        sa.Column("candidate_artifact_id", sa.UUID(), nullable=False),
        sa.Column("validation_artifact_id", sa.UUID(), nullable=False),
        sa.Column("criterion_evaluation_set_artifact_id", sa.UUID(), nullable=False),
        sa.Column("completion_gate_decision_artifact_id", sa.UUID(), nullable=False),
        sa.Column("renderer_profile", sa.String(length=64), nullable=False),
        sa.Column("renderer_profile_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("result_commit_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("commit_receipt_id", sa.UUID(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "answer_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_task_result_answer_sha256_lower_hex"),
        ),
        sa.CheckConstraint(
            "answer_status IN ('COMPLETE','PARTIAL','INSUFFICIENT_EVIDENCE')",
            name=op.f("ck_task_result_answer_status_allowed"),
        ),
        sa.CheckConstraint(
            "citation_count BETWEEN 0 AND 200",
            name=op.f("ck_task_result_citation_count_range"),
        ),
        sa.CheckConstraint(
            "renderer_profile_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_task_result_renderer_profile_digest_lower_hex"),
        ),
        sa.CheckConstraint(
            "result_artifact_count BETWEEN 0 AND 50",
            name=op.f("ck_task_result_result_artifact_count_range"),
        ),
        sa.CheckConstraint(
            "result_commit_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_task_result_result_commit_digest_lower_hex"),
        ),
        sa.CheckConstraint(
            "result_version = 1",
            name=op.f("ck_task_result_result_version_first"),
        ),
        sa.CheckConstraint("row_version >= 1", name=op.f("ck_task_result_row_version_positive")),
        sa.ForeignKeyConstraint(
            ["answer_artifact_id"],
            ["content.artifact.artifact_id"],
            name=op.f("fk_task_result_answer_artifact_id_artifact"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_artifact_id"],
            ["content.artifact.artifact_id"],
            name=op.f("fk_task_result_candidate_artifact_id_artifact"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["completion_gate_decision_artifact_id"],
            ["content.artifact.artifact_id"],
            name=op.f("fk_task_result_completion_gate_decision_artifact_id_artifact"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["criterion_evaluation_set_artifact_id"],
            ["content.artifact.artifact_id"],
            name=op.f("fk_task_result_criterion_evaluation_set_artifact_id_artifact"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["result_envelope_artifact_id"],
            ["content.artifact.artifact_id"],
            name=op.f("fk_task_result_result_envelope_artifact_id_artifact"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["workflow.intelligent_task.id"],
            name=op.f("fk_task_result_task_id_intelligent_task"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["validation_artifact_id"],
            ["content.artifact.artifact_id"],
            name=op.f("fk_task_result_validation_artifact_id_artifact"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("task_id", "result_version", name=op.f("pk_task_result")),
        schema="workflow",
    )


def downgrade() -> None:
    # 终态结果是任务生命周期事实；有数据时只能先备份/对账，不能以降级静默删除。
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM workflow.task_result) THEN "
            "RAISE EXCEPTION 'TASK_RESULT_DOWNGRADE_REQUIRES_BACKUP_RESTORE'; "
            "END IF; END $$"
        )
    )
    op.drop_table("task_result", schema="workflow")
