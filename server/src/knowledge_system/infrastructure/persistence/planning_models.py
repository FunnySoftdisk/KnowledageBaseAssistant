"""DEV-01C Goal与不可变Plan定义的SQLAlchemy映射。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TaskGoalUnderstandingRecord(Base):
    __tablename__ = "task_goal_understanding"
    __table_args__ = (
        UniqueConstraint("task_id", "input_id", "task_attempt_id"),
        CheckConstraint(
            "contract_version = 'supervisor_goal_understanding_v1'",
            name="contract_version_v1",
        ),
        CheckConstraint(
            "prompt_profile = 'supervisor-goal-understanding-v1'",
            name="prompt_profile_v1",
        ),
        CheckConstraint("model_alias = 'planning_strong'", name="model_alias_planning"),
        CheckConstraint("gate_profile = 'clarification_gate_v1'", name="gate_profile_v1"),
        CheckConstraint(
            "gate_outcome IN ('READY_TO_PLAN','REQUEST_USER_INPUT')",
            name="gate_outcome_allowed",
        ),
        CheckConstraint("subgoal_count BETWEEN 0 AND 8", name="subgoal_count_range"),
        CheckConstraint("criterion_count BETWEEN 1 AND 12", name="criterion_count_range"),
        CheckConstraint("assumption_count BETWEEN 0 AND 8", name="assumption_count_range"),
        CheckConstraint("blocking_issue_count BETWEEN 0 AND 3", name="blocking_issue_count_range"),
        CheckConstraint(
            "gate_outcome <> 'REQUEST_USER_INPUT' OR blocking_issue_count BETWEEN 1 AND 3",
            name="gate_blocking_issue_shape",
        ),
        CheckConstraint(
            "prompt_profile_digest ~ '^[0-9a-f]{64}$' AND "
            "context_digest ~ '^[0-9a-f]{64}$' AND "
            "understanding_digest ~ '^[0-9a-f]{64}$' AND "
            "gate_profile_digest ~ '^[0-9a-f]{64}$' AND "
            "gate_digest ~ '^[0-9a-f]{64}$'",
            name="digests_lower_hex",
        ),
        {"schema": "workflow"},
    )

    understanding_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT")
    )
    input_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.task_input_snapshot.input_id", ondelete="RESTRICT"),
    )
    task_attempt_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.task_attempt.id", ondelete="RESTRICT"),
    )
    contract_version: Mapped[str] = mapped_column(String(64))
    prompt_profile: Mapped[str] = mapped_column(String(64))
    prompt_profile_digest: Mapped[str] = mapped_column(CHAR(64))
    model_alias: Mapped[str] = mapped_column(String(64))
    goal_context_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    context_digest: Mapped[str] = mapped_column(CHAR(64))
    raw_model_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    goal_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    understanding_digest: Mapped[str] = mapped_column(CHAR(64))
    gate_profile: Mapped[str] = mapped_column(String(64))
    gate_profile_digest: Mapped[str] = mapped_column(CHAR(64))
    gate_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    gate_digest: Mapped[str] = mapped_column(CHAR(64))
    gate_outcome: Mapped[str] = mapped_column(String(32))
    subgoal_count: Mapped[int] = mapped_column(Integer)
    criterion_count: Mapped[int] = mapped_column(Integer)
    assumption_count: Mapped[int] = mapped_column(Integer)
    blocking_issue_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskPlanVersionRecord(Base):
    __tablename__ = "task_plan_version"
    __table_args__ = (
        PrimaryKeyConstraint("task_id", "plan_version"),
        CheckConstraint("plan_version >= 1", name="plan_version_positive"),
        CheckConstraint(
            "predecessor_version IS NULL OR "
            "(predecessor_version >= 1 AND predecessor_version < plan_version)",
            name="predecessor_before_plan",
        ),
        CheckConstraint(
            "status IN ('ACTIVE','SUPERSEDED','COMPLETED','ABANDONED')",
            name="status_allowed",
        ),
        CheckConstraint("compiled_plan_digest ~ '^[0-9a-f]{64}$'", name="digest_lower_hex"),
        CheckConstraint(
            "(status = 'ACTIVE' AND activated_at IS NOT NULL AND completed_at IS NULL) OR "
            "(status = 'SUPERSEDED' AND activated_at IS NOT NULL AND completed_at IS NOT NULL) OR "
            "(status = 'COMPLETED' AND activated_at IS NOT NULL AND completed_at IS NOT NULL) OR "
            "(status = 'ABANDONED' AND completed_at IS NOT NULL)",
            name="status_time_shape",
        ),
        Index(
            "uq_task_plan_version_active",
            "task_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        {"schema": "workflow"},
    )

    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT")
    )
    plan_version: Mapped[int] = mapped_column(Integer)
    predecessor_version: Mapped[int | None] = mapped_column(Integer)
    input_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.task_input_snapshot.input_id", ondelete="RESTRICT"),
    )
    task_attempt_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.task_attempt.id", ondelete="RESTRICT"),
    )
    goal_understanding_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.task_goal_understanding.understanding_id", ondelete="RESTRICT"),
    )
    planning_context_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    capability_planning_view_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    raw_model_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    validated_draft_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    validation_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    compiled_plan_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    compiled_plan_digest: Mapped[str] = mapped_column(CHAR(64))
    revision_reason: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    created_by_agent_instance_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.agent_instance.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskPlanItemDefinitionRecord(Base):
    __tablename__ = "task_plan_item_definition"
    __table_args__ = (
        PrimaryKeyConstraint("task_id", "plan_version", "plan_item_id"),
        ForeignKeyConstraint(
            ["task_id", "plan_version"],
            ["workflow.task_plan_version.task_id", "workflow.task_plan_version.plan_version"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("task_id", "plan_version", "source_local_id"),
        CheckConstraint(
            "source_local_id ~ '^[a-z][a-z0-9_-]{0,63}$'", name="source_local_id_valid"
        ),
        CheckConstraint(
            "executor_requirement IN ('SUPERVISOR','SPECIALIST','DYNAMIC_ALLOWED')",
            name="executor_requirement_allowed",
        ),
        CheckConstraint("initial_state IN ('OPEN','READY')", name="initial_state_allowed"),
        CheckConstraint("definition_digest ~ '^[0-9a-f]{64}$'", name="digest_lower_hex"),
        {"schema": "workflow"},
    )

    task_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    plan_version: Mapped[int] = mapped_column(Integer)
    plan_item_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    source_local_id: Mapped[str] = mapped_column(String(64))
    objective: Mapped[str] = mapped_column(Text)
    executor_requirement: Mapped[str] = mapped_column(String(32))
    capability_requirements_json: Mapped[list[object]] = mapped_column(JSONB)
    candidate_capability_refs_json: Mapped[list[object]] = mapped_column(JSONB)
    input_artifact_refs_json: Mapped[list[object]] = mapped_column(JSONB)
    output_schema_ref_json: Mapped[dict[str, object]] = mapped_column(JSONB)
    depends_on_item_ids_json: Mapped[list[object]] = mapped_column(JSONB)
    iteration_policy_json: Mapped[dict[str, object]] = mapped_column(JSONB)
    initial_state: Mapped[str] = mapped_column(String(16))
    definition_digest: Mapped[str] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskPlanHypothesisRecord(Base):
    __tablename__ = "task_plan_hypothesis"
    __table_args__ = (
        PrimaryKeyConstraint("task_id", "plan_version", "hypothesis_id"),
        ForeignKeyConstraint(
            ["task_id", "plan_version"],
            ["workflow.task_plan_version.task_id", "workflow.task_plan_version.plan_version"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("task_id", "plan_version", "source_local_id"),
        CheckConstraint(
            "source_local_id ~ '^[a-z][a-z0-9_-]{0,63}$'", name="source_local_id_valid"
        ),
        CheckConstraint("initial_status = 'OPEN'", name="initial_status_open"),
        CheckConstraint("hypothesis_digest ~ '^[0-9a-f]{64}$'", name="digest_lower_hex"),
        {"schema": "workflow"},
    )

    task_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    plan_version: Mapped[int] = mapped_column(Integer)
    hypothesis_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    source_local_id: Mapped[str] = mapped_column(String(64))
    statement: Mapped[str] = mapped_column(Text)
    initial_status: Mapped[str] = mapped_column(String(16))
    hypothesis_digest: Mapped[str] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskCompiledCriterionRecord(Base):
    __tablename__ = "task_compiled_criterion"
    __table_args__ = (
        PrimaryKeyConstraint("task_id", "plan_version", "criterion_id"),
        ForeignKeyConstraint(
            ["task_id", "plan_version"],
            ["workflow.task_plan_version.task_id", "workflow.task_plan_version.plan_version"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "source_kind IN ('USER','OUTPUT_CONTRACT','POLICY','GOAL','PLAN')",
            name="source_kind_allowed",
        ),
        CheckConstraint(
            "requirement_level IN ('REQUIRED','SUPPORTING')",
            name="requirement_level_allowed",
        ),
        CheckConstraint(
            "completion_role IN ('CORE_DELIVERABLE','REQUIRED_AUXILIARY','SUPPORTING')",
            name="completion_role_allowed",
        ),
        CheckConstraint(
            "evaluation_mode IN ('DETERMINISTIC','EVIDENCE_GROUNDED_SEMANTIC')",
            name="evaluation_mode_allowed",
        ),
        CheckConstraint(
            "completion_role <> 'CORE_DELIVERABLE' OR requirement_level = 'REQUIRED'",
            name="core_deliverable_required",
        ),
        CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="digest_lower_hex"),
        {"schema": "workflow"},
    )

    task_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    plan_version: Mapped[int] = mapped_column(Integer)
    criterion_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    statement: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(32))
    source_refs: Mapped[list[object]] = mapped_column(JSONB)
    requirement_level: Mapped[str] = mapped_column(String(16))
    completion_role: Mapped[str] = mapped_column(String(32))
    protected: Mapped[bool] = mapped_column(Boolean)
    evaluation_mode: Mapped[str] = mapped_column(String(40))
    predicate: Mapped[dict[str, object]] = mapped_column(JSONB)
    applicability: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    evaluator_profile: Mapped[dict[str, object]] = mapped_column(JSONB)
    digest: Mapped[str] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanItemRuntimeRecord(Base):
    __tablename__ = "plan_item_runtime"
    __table_args__ = (
        PrimaryKeyConstraint("task_id", "plan_version", "plan_item_id"),
        ForeignKeyConstraint(
            ["task_id", "plan_version", "plan_item_id"],
            [
                "workflow.task_plan_item_definition.task_id",
                "workflow.task_plan_item_definition.plan_version",
                "workflow.task_plan_item_definition.plan_item_id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('OPEN','READY','RUNNING','BLOCKED','COMPLETED','ABANDONED')",
            name="status_allowed",
        ),
        CheckConstraint("current_iteration BETWEEN 0 AND 3", name="iteration_range"),
        CheckConstraint("row_version >= 1", name="row_version_positive"),
        CheckConstraint(
            "(status IN ('OPEN','READY','BLOCKED') AND completed_at IS NULL) OR "
            "(status = 'RUNNING' AND started_at IS NOT NULL AND completed_at IS NULL) OR "
            "(status IN ('COMPLETED','ABANDONED') AND completed_at IS NOT NULL)",
            name="status_time_shape",
        ),
        {"schema": "workflow"},
    )

    task_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    plan_version: Mapped[int] = mapped_column(Integer)
    plan_item_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(16))
    current_iteration: Mapped[int] = mapped_column(Integer)
    active_task_attempt_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.task_attempt.id", ondelete="RESTRICT"),
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
