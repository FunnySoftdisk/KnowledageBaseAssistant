"""`workflow.task_runtime_state` 的任务级运行时闭环权威映射（一 Task 一行）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TaskRuntimeStateRecord(Base):
    __tablename__ = "task_runtime_state"
    __table_args__ = (
        CheckConstraint("work_version >= 1", name="work_version_positive"),
        CheckConstraint("execution_generation >= 0", name="execution_generation_nonnegative"),
        CheckConstraint("cancellation_generation >= 0", name="cancellation_generation_nonnegative"),
        CheckConstraint("same_loop_no_progress >= 0", name="same_loop_no_progress_nonnegative"),
        CheckConstraint("consecutive_no_progress >= 0", name="consecutive_no_progress_nonnegative"),
        CheckConstraint("epoch_no_progress >= 0", name="epoch_no_progress_nonnegative"),
        CheckConstraint("task_no_progress >= 0", name="task_no_progress_nonnegative"),
        CheckConstraint("task_replan_count >= 0", name="task_replan_count_nonnegative"),
        CheckConstraint("row_version >= 1", name="row_version_positive"),
        CheckConstraint(
            "convergence_action IS NULL OR convergence_action IN "
            "('CONTINUE_LOOP','REPLAN','CONVERGE')",
            name="convergence_action_allowed",
        ),
        CheckConstraint(
            "current_loop_fingerprint IS NULL OR current_loop_fingerprint ~ '^[0-9a-f]{64}$'",
            name="loop_fingerprint_lower_hex",
        ),
        {"schema": "workflow"},
    )

    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    work_version: Mapped[int] = mapped_column(Integer, default=1)
    execution_generation: Mapped[int] = mapped_column(BigInteger, default=0)
    cancellation_generation: Mapped[int] = mapped_column(BigInteger, default=0)
    next_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocking_effect_refs_json: Mapped[list[object]] = mapped_column(JSONB, default=list)
    blocking_issue_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    ready_set_ref_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    current_loop_fingerprint: Mapped[str | None] = mapped_column(CHAR(64))
    same_loop_no_progress: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_no_progress: Mapped[int] = mapped_column(Integer, default=0)
    epoch_no_progress: Mapped[int] = mapped_column(Integer, default=0)
    task_no_progress: Mapped[int] = mapped_column(Integer, default=0)
    episode_replan_used: Mapped[bool] = mapped_column(Boolean, default=False)
    task_replan_count: Mapped[int] = mapped_column(Integer, default=0)
    episode_seen_loop_fingerprints_json: Mapped[list[object]] = mapped_column(JSONB, default=list)
    convergence_action: Mapped[str | None] = mapped_column(String(16))
    replan_required: Mapped[bool] = mapped_column(Boolean, default=False)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


__all__ = ["TaskRuntimeStateRecord"]
