"""DEV-01C Attempt直接运行依赖表：AgentInstance、TaskAttempt与Context Pack。

Budget Reservation与Capability Binding Snapshot的字段契约尚未冻结（权威文档仅给出
口语化字段集合，未命名五项预留列与披露边界列），因此TaskAttempt对二者的引用保持
FK_DEFERRED裸UUID列，待对应表投影批准后在后续迁移闭合。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
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


class AgentInstanceRecord(Base):
    __tablename__ = "agent_instance"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('SUPERVISOR','BUILT_IN','EPHEMERAL')", name="kind_allowed"
        ),
        CheckConstraint(
            "status IN ('CREATED','ACTIVE','SUSPENDED','REVOKED','EXPIRED','COMPLETED')",
            name="status_allowed",
        ),
        CheckConstraint(
            "kind <> 'EPHEMERAL' OR (parent_agent_instance_id IS NOT NULL AND "
            "expires_at IS NOT NULL)",
            name="ephemeral_requires_parent_and_expiry",
        ),
        CheckConstraint(
            "created_reason_hash ~ '^[0-9a-f]{64}$'", name="created_reason_hash_lower_hex"
        ),
        Index(
            "uq_agent_instance_reuse_key",
            "task_id",
            "parent_agent_instance_id",
            "manifest_id",
            "manifest_version",
            "created_reason_hash",
            unique=True,
            postgresql_where=text("parent_agent_instance_id IS NOT NULL"),
        ),
        {"schema": "workflow"},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT")
    )
    parent_agent_instance_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.agent_instance.id", ondelete="RESTRICT"),
    )
    manifest_id: Mapped[str] = mapped_column(String(128))
    manifest_version: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    delegated_scope: Mapped[dict[str, object]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(24))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_reason: Mapped[str] = mapped_column(Text)
    created_reason_hash: Mapped[str] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskAttemptRecord(Base):
    __tablename__ = "task_attempt"
    __table_args__ = (
        CheckConstraint(
            "attempt_kind IN ('GOAL_UNDERSTANDING','PLAN','REPLAN','ACTION','FINAL_SYNTHESIS')",
            name="attempt_kind_allowed",
        ),
        CheckConstraint(
            "status IN ('CREATED','RUNNING','SUSPENDED','COMPLETED','FAILED','CANCELLED')",
            name="status_allowed",
        ),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        CheckConstraint("lease_generation >= 0", name="lease_generation_nonnegative"),
        CheckConstraint("row_version >= 1", name="row_version_positive"),
        CheckConstraint(
            "reservation_version IS NULL OR reservation_version >= 1",
            name="reservation_version_positive",
        ),
        CheckConstraint(
            "external_effect_state IN ('NO_EFFECT','EFFECT_CONFIRMED','OUTCOME_UNKNOWN',"
            "'RECONCILING')",
            name="external_effect_state_allowed",
        ),
        CheckConstraint(
            "suspension_kind IS NULL OR suspension_kind IN "
            "('APPROVAL','USER_INPUT','EXTERNAL_RESULT','MIXED')",
            name="suspension_kind_allowed",
        ),
        CheckConstraint(
            "failure_class IS NULL OR failure_class IN ('BUSINESS','DEPENDENCY','CONTRACT',"
            "'POLICY','BUDGET','RESOURCE','EXTERNAL_EFFECT')",
            name="failure_class_allowed",
        ),
        CheckConstraint(
            "agent_registration_key ~ '^[a-z][a-z0-9-]{0,63}$'",
            name="agent_registration_key_shape",
        ),
        CheckConstraint(
            "manifest_digest ~ '^[0-9a-f]{64}$' AND "
            "context_pack_hash ~ '^[0-9a-f]{64}$' AND "
            "grant_snapshot_digest ~ '^[0-9a-f]{64}$' AND "
            "input_digest ~ '^[0-9a-f]{64}$'",
            name="digests_lower_hex",
        ),
        CheckConstraint(
            "(result_digest IS NULL OR result_digest ~ '^[0-9a-f]{64}$') AND "
            "(capability_binding_snapshot_digest IS NULL OR "
            "capability_binding_snapshot_digest ~ '^[0-9a-f]{64}$') AND "
            "(final_synthesis_snapshot_digest IS NULL OR "
            "final_synthesis_snapshot_digest ~ '^[0-9a-f]{64}$')",
            name="nullable_digests_lower_hex",
        ),
        CheckConstraint(
            "temporal_child_workflow_id = "
            "'agent-attempt-v1-' || lower(task_id::text) || '-' || lower(id::text)",
            name="child_workflow_id_deterministic",
        ),
        CheckConstraint(
            "(attempt_kind = 'GOAL_UNDERSTANDING' AND plan_version IS NULL) OR "
            "(attempt_kind <> 'GOAL_UNDERSTANDING' AND plan_version IS NOT NULL)",
            name="plan_version_shape",
        ),
        CheckConstraint(
            "(attempt_kind = 'ACTION' AND plan_item_id IS NOT NULL AND "
            "action_run_group_id IS NOT NULL) OR "
            "(attempt_kind <> 'ACTION' AND plan_item_id IS NULL AND "
            "action_run_group_id IS NULL)",
            name="action_branch_shape",
        ),
        CheckConstraint(
            "(attempt_kind = 'REPLAN' AND replan_admission_id IS NOT NULL AND "
            "replan_event_high_watermark IS NOT NULL AND "
            "replan_context_artifact_id IS NOT NULL AND "
            "execution_snapshot_artifact_id IS NOT NULL) OR "
            "(attempt_kind <> 'REPLAN' AND replan_admission_id IS NULL AND "
            "replan_event_high_watermark IS NULL AND "
            "replan_context_artifact_id IS NULL AND "
            "execution_snapshot_artifact_id IS NULL)",
            name="replan_branch_shape",
        ),
        CheckConstraint(
            "(attempt_kind = 'FINAL_SYNTHESIS' AND final_synthesis_snapshot_id IS NOT NULL "
            "AND final_synthesis_snapshot_digest IS NOT NULL AND "
            "runtime_decision_id IS NOT NULL AND current_work_version IS NOT NULL) OR "
            "(attempt_kind <> 'FINAL_SYNTHESIS' AND final_synthesis_snapshot_id IS NULL "
            "AND final_synthesis_snapshot_digest IS NULL AND runtime_decision_id IS NULL "
            "AND current_work_version IS NULL)",
            name="final_branch_shape",
        ),
        UniqueConstraint("temporal_child_workflow_id"),
        UniqueConstraint("pydantic_run_id"),
        Index(
            "uq_task_attempt_action_group_attempt_no",
            "action_run_group_id",
            "attempt_no",
            unique=True,
            postgresql_where=text("attempt_kind = 'ACTION'"),
        ),
        Index(
            "uq_task_attempt_task_kind_attempt_no",
            "task_id",
            "attempt_kind",
            "attempt_no",
            unique=True,
            postgresql_where=text("attempt_kind <> 'ACTION'"),
        ),
        {"schema": "workflow"},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT")
    )
    input_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.task_input_snapshot.input_id", ondelete="RESTRICT"),
    )
    plan_version: Mapped[int | None] = mapped_column(Integer)
    # task_plan_item_definition主键为复合，plan_item_id无法单独外键引用。
    plan_item_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    action_run_group_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    attempt_kind: Mapped[str] = mapped_column(String(24))
    attempt_no: Mapped[int] = mapped_column(Integer)
    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.conversation.id", ondelete="RESTRICT")
    )
    agent_instance_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.agent_instance.id", ondelete="RESTRICT")
    )
    agent_registration_key: Mapped[str] = mapped_column(String(64))
    manifest_id: Mapped[str] = mapped_column(String(128))
    manifest_version: Mapped[str] = mapped_column(String(64))
    manifest_digest: Mapped[str] = mapped_column(CHAR(64))
    context_pack_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.context_pack.id", ondelete="RESTRICT")
    )
    context_pack_hash: Mapped[str] = mapped_column(CHAR(64))
    grant_snapshot_digest: Mapped[str] = mapped_column(CHAR(64))
    # capability_binding_snapshot字段契约未冻结。
    capability_binding_snapshot_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    capability_binding_snapshot_digest: Mapped[str | None] = mapped_column(CHAR(64))
    # budget_reservation字段契约未冻结。
    budget_reservation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    reservation_version: Mapped[int | None] = mapped_column(Integer)
    input_schema_version: Mapped[str] = mapped_column(String(64))
    input_digest: Mapped[str] = mapped_column(CHAR(64))
    pydantic_run_id: Mapped[str] = mapped_column(String(128))
    resumes_task_attempt_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.task_attempt.id", ondelete="RESTRICT")
    )
    temporal_child_workflow_id: Mapped[str] = mapped_column(String(255))
    temporal_child_run_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    lease_generation: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(32))
    result_kind: Mapped[str | None] = mapped_column(String(64))
    result_schema_version: Mapped[str | None] = mapped_column(String(64))
    result_digest: Mapped[str | None] = mapped_column(CHAR(64))
    suspension_kind: Mapped[str | None] = mapped_column(String(24))
    error_code: Mapped[str | None] = mapped_column(String(64))
    failure_class: Mapped[str | None] = mapped_column(String(32))
    diagnostic_artifact_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    external_effect_state: Mapped[str] = mapped_column(String(24))
    # replan_admission/runtime_decision/final_synthesis_snapshot字段契约未冻结。
    replan_admission_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    replan_event_high_watermark: Mapped[int | None] = mapped_column(BigInteger)
    replan_context_artifact_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    execution_snapshot_artifact_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    final_synthesis_snapshot_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    final_synthesis_snapshot_digest: Mapped[str | None] = mapped_column(CHAR(64))
    runtime_decision_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    current_work_version: Mapped[int | None] = mapped_column(BigInteger)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ContextPackRecord(Base):
    __tablename__ = "context_pack"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "action_attempt_id",
            "input_digest",
            "context_policy_version",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "state IN ('ASSEMBLING','READY','CONSUMED','FAILED','INVALIDATED_BEFORE_USE')",
            name="state_allowed",
        ),
        CheckConstraint(
            "conversation_memory_version >= 0 AND user_private_memory_version >= 0 "
            "AND task_memory_version >= 0",
            name="memory_versions_nonnegative",
        ),
        CheckConstraint(
            "max_context >= 0 AND reserved_output >= 0 AND protocol_overhead >= 0 "
            "AND safety_margin >= 0 AND input_tokens >= 0",
            name="budgets_nonnegative",
        ),
        CheckConstraint(
            "chat_template_hash ~ '^[0-9a-f]{64}$' AND pack_hash ~ '^[0-9a-f]{64}$' "
            "AND input_digest ~ '^[0-9a-f]{64}$'",
            name="digests_lower_hex",
        ),
        {"schema": "workflow"},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT")
    )
    action_attempt_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_by_attempt_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.task_attempt.id", ondelete="RESTRICT", use_alter=True),
    )
    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.conversation.id", ondelete="RESTRICT")
    )
    actor_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.user_account.id", ondelete="RESTRICT")
    )
    # iam.authorization_snapshot字段契约尚未批准。
    authorization_ref_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    authorization_version: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(24))
    context_policy_version: Mapped[str] = mapped_column(String(64))
    conversation_memory_version: Mapped[int] = mapped_column(BigInteger)
    user_private_memory_version: Mapped[int] = mapped_column(BigInteger)
    task_memory_version: Mapped[int] = mapped_column(BigInteger)
    runtime_version: Mapped[str] = mapped_column(String(64))
    plan_version: Mapped[int | None] = mapped_column(Integer)
    agent_manifest_version: Mapped[str] = mapped_column(String(64))
    model_revision: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(64))
    tool_schema_versions: Mapped[dict[str, object]] = mapped_column(JSONB)
    output_schema_id: Mapped[str | None] = mapped_column(String(128))
    output_schema_version: Mapped[str | None] = mapped_column(String(64))
    tokenizer_revision: Mapped[str] = mapped_column(String(128))
    chat_template_hash: Mapped[str] = mapped_column(CHAR(64))
    max_context: Mapped[int] = mapped_column(Integer)
    reserved_output: Mapped[int] = mapped_column(Integer)
    protocol_overhead: Mapped[int] = mapped_column(Integer)
    safety_margin: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer)
    body_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    pack_hash: Mapped[str] = mapped_column(CHAR(64))
    input_digest: Mapped[str] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContextPackItemRecord(Base):
    __tablename__ = "context_pack_item"
    __table_args__ = (
        PrimaryKeyConstraint("context_pack_id", "position"),
        ForeignKeyConstraint(
            ["context_pack_id"],
            ["workflow.context_pack.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("position >= 1", name="position_positive"),
        CheckConstraint("token_count >= 0", name="token_count_nonnegative"),
        CheckConstraint("compression_level >= 0", name="compression_level_nonnegative"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_lower_hex"),
        Index("ix_context_pack_item_source", "source_type", "source_id"),
        {"schema": "workflow"},
    )

    context_pack_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    position: Mapped[int] = mapped_column(Integer)
    item_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    item_type: Mapped[str] = mapped_column(String(64))
    source_type: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    source_version: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(CHAR(64))
    token_count: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer)
    rank_score: Mapped[float] = mapped_column(Float)
    compression_level: Mapped[int] = mapped_column(Integer)
    selection_reason: Mapped[str] = mapped_column(String(256))
    trust: Mapped[str] = mapped_column(String(24))
    bucket: Mapped[str] = mapped_column(String(64))
