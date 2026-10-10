"""DEV-06 `workflow.task_result` 终态结果表。

依据数据库设计§9.3.2：与 COMPLETED Task 一对一，`(task_id, result_version)` 复合主键且首版
`result_version=1` 不可覆盖；保存 Answer 内容寻址 Artifact 与 SHA、规范化 Envelope Artifact、
Citation/Result Artifact 计数、Criterion/Constraint/Degradation 快照、Final Snapshot/Attempt、
Candidate/Validation/Evaluation/Gate Artifact、Renderer Profile 与 Commit Digest/Receipt。

`final_synthesis_snapshot`/`final_synthesis_attempt`/`task_result_commit_receipt` 尚未冻结，
`final_snapshot_id`/`final_attempt_id`/`commit_receipt_id` 保持裸 UUID 延迟外键；
`intelligent_task`/`content.artifact` 已冻结，落地真实外键。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

TASK_RESULT_CONTRACT_VERSION = "task_result_v1"


class TaskResultRecord(Base):
    __tablename__ = "task_result"
    __table_args__ = (
        PrimaryKeyConstraint("task_id", "result_version"),
        CheckConstraint("result_version = 1", name="result_version_first"),
        CheckConstraint(
            "answer_status IN ('COMPLETE','PARTIAL','INSUFFICIENT_EVIDENCE')",
            name="answer_status_allowed",
        ),
        CheckConstraint("citation_count BETWEEN 0 AND 200", name="citation_count_range"),
        CheckConstraint(
            "result_artifact_count BETWEEN 0 AND 50", name="result_artifact_count_range"
        ),
        CheckConstraint("answer_sha256 ~ '^[0-9a-f]{64}$'", name="answer_sha256_lower_hex"),
        CheckConstraint(
            "renderer_profile_digest ~ '^[0-9a-f]{64}$'", name="renderer_profile_digest_lower_hex"
        ),
        CheckConstraint(
            "result_commit_digest ~ '^[0-9a-f]{64}$'", name="result_commit_digest_lower_hex"
        ),
        CheckConstraint("row_version >= 1", name="row_version_positive"),
        {"schema": "workflow"},
    )

    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT"),
    )
    contract_version: Mapped[str] = mapped_column(String(32))
    result_version: Mapped[int] = mapped_column(Integer)
    answer_status: Mapped[str] = mapped_column(String(32))
    answer_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    answer_sha256: Mapped[str] = mapped_column(CHAR(64))
    result_envelope_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    citation_count: Mapped[int] = mapped_column(Integer)
    result_artifact_count: Mapped[int] = mapped_column(Integer)
    criterion_checks: Mapped[list[object]] = mapped_column(JSONB)
    constraint_checks: Mapped[list[object]] = mapped_column(JSONB)
    degradation_flags: Mapped[list[str]] = mapped_column(ARRAY(Text))
    # final_synthesis_snapshot / final_synthesis_attempt 尚未冻结。
    final_snapshot_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    final_attempt_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    candidate_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    validation_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    criterion_evaluation_set_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    completion_gate_decision_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    renderer_profile: Mapped[str] = mapped_column(String(64))
    renderer_profile_digest: Mapped[str] = mapped_column(CHAR(64))
    result_commit_digest: Mapped[str] = mapped_column(CHAR(64))
    # task_result_commit_receipt 尚未冻结。
    commit_receipt_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, default=1)


__all__ = ["TASK_RESULT_CONTRACT_VERSION", "TaskResultRecord"]
