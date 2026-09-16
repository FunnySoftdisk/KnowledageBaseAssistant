"""DEV-01任务入口与事务发件表的SQLAlchemy映射。"""

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


class ConversationMessageRecord(Base):
    __tablename__ = "conversation_message"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", "revision"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_lower_hex"),
        CheckConstraint("state IN ('ACTIVE','SUPERSEDED','RETRACTED')", name="state_allowed"),
        CheckConstraint(
            "(message_kind IN ('USER_QUERY','USER_APPEND') AND role = 'USER') OR "
            "(message_kind = 'ASSISTANT_FINAL' AND role = 'ASSISTANT') OR "
            "(message_kind IN ('TASK_FAILED_NOTICE','TASK_CANCELLED_NOTICE',"
            "'TASK_RERUN_NOTICE') AND role = 'SYSTEM_NOTICE')",
            name="kind_role_match",
        ),
        CheckConstraint(
            "message_kind NOT IN ('USER_QUERY','USER_APPEND','ASSISTANT_FINAL',"
            "'TASK_FAILED_NOTICE','TASK_CANCELLED_NOTICE','TASK_RERUN_NOTICE') "
            "OR task_id IS NOT NULL",
            name="task_kind_has_task",
        ),
        Index(
            "uq_conversation_message_active_revision",
            "conversation_id",
            "sequence",
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
        ),
        Index(
            "uq_conversation_message_active_task_kind",
            "task_id",
            "message_kind",
            unique=True,
            postgresql_where=text(
                "state = 'ACTIVE' AND message_kind IN ('USER_QUERY','ASSISTANT_FINAL')"
            ),
        ),
        {"schema": "workflow"},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.conversation.id", ondelete="RESTRICT")
    )
    role: Mapped[str] = mapped_column(String(16))
    message_kind: Mapped[str] = mapped_column(String(32))
    content_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    task_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT", use_alter=True),
    )
    sequence: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    supersedes_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.conversation_message.id", ondelete="RESTRICT"),
    )
    content_hash: Mapped[str] = mapped_column(CHAR(64))
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IntelligentTaskRecord(Base):
    __tablename__ = "intelligent_task"
    __table_args__ = (
        UniqueConstraint("temporal_workflow_id"),
        CheckConstraint(
            "status IN ('QUEUED','PLANNING','RUNNING','REPLANNING','FINALIZING',"
            "'WAITING_USER_INPUT','WAITING_APPROVAL','RETRY_WAIT','CANCEL_REQUESTED',"
            "'COMPLETED','FAILED','CANCELLED')",
            name="status_allowed",
        ),
        CheckConstraint(
            "active_plan_version IS NULL OR active_plan_version >= 1", name="plan_positive"
        ),
        CheckConstraint("last_event_sequence >= 0", name="event_sequence_nonnegative"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("deadline_at > created_at", name="deadline_after_creation"),
        CheckConstraint(
            "rerun_of_task_id IS NULL OR rerun_of_task_id <> id", name="rerun_not_self"
        ),
        CheckConstraint("jsonb_typeof(budget) = 'object'", name="budget_object"),
        CheckConstraint("jsonb_typeof(budget_used) = 'object'", name="budget_used_object"),
        CheckConstraint(
            "temporal_workflow_id = 'agent-task-v1-' || lower(id::text)",
            name="temporal_workflow_id_deterministic",
        ),
        Index("ix_intelligent_task_owner_created", "owner_id", "created_at", "id"),
        Index("ix_intelligent_task_status_created", "status", "created_at"),
        Index("ix_intelligent_task_conversation", "conversation_id"),
        Index("ix_intelligent_task_rerun", "rerun_of_task_id"),
        Index(
            "ix_intelligent_task_open_deadline",
            "deadline_at",
            "id",
            postgresql_where=text("status NOT IN ('COMPLETED','FAILED','CANCELLED')"),
        ),
        {"schema": "workflow"},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.organization.id", ondelete="RESTRICT")
    )
    owner_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.user_account.id", ondelete="RESTRICT")
    )
    rerun_of_task_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT"),
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.conversation.id", ondelete="RESTRICT")
    )
    query_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    initial_input_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "workflow.task_input_snapshot.input_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
    )
    current_input_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "workflow.task_input_snapshot.input_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
    )
    # iam.authorization_snapshot的完整字段尚未批准，因此本批只建列。
    authorization_ref_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(32), server_default="QUEUED")
    active_plan_version: Mapped[int | None] = mapped_column(Integer)
    temporal_workflow_id: Mapped[str] = mapped_column(String(255))
    last_event_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    budget: Mapped[dict[str, object]] = mapped_column(JSONB)
    budget_used: Mapped[dict[str, object]] = mapped_column(JSONB)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result_artifact_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TaskInputSnapshotRecord(Base):
    __tablename__ = "task_input_snapshot"
    __table_args__ = (
        UniqueConstraint("task_id", "input_revision"),
        CheckConstraint("input_revision >= 1", name="input_revision_positive"),
        CheckConstraint("contract_version = 'task_input_snapshot_v1'", name="contract_version_v1"),
        CheckConstraint("snapshot_digest ~ '^[0-9a-f]{64}$'", name="snapshot_digest_lower_hex"),
        CheckConstraint("network_policy IN ('DENY','ASK','ALLOW')", name="network_policy_allowed"),
        CheckConstraint(
            "network_policy_source IN ('UI_FIELD','QUERY_SPAN','SYSTEM_DEFAULT')",
            name="network_policy_source_allowed",
        ),
        {"schema": "workflow"},
    )

    input_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT")
    )
    input_revision: Mapped[int] = mapped_column(Integer)
    message_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.conversation_message.id", ondelete="RESTRICT")
    )
    message_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    actor_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.user_account.id", ondelete="RESTRICT")
    )
    organization_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.organization.id", ondelete="RESTRICT")
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.conversation.id", ondelete="RESTRICT")
    )
    attachment_binding_refs_json: Mapped[list[object]] = mapped_column(JSONB)
    knowledge_scope_json: Mapped[dict[str, object]] = mapped_column(JSONB)
    network_policy: Mapped[str] = mapped_column(String(16))
    network_policy_source: Mapped[str] = mapped_column(String(32))
    output_contract_json: Mapped[dict[str, object]] = mapped_column(JSONB)
    selected_resource_refs_json: Mapped[list[object]] = mapped_column(JSONB)
    task_constraint_refs_json: Mapped[list[object]] = mapped_column(JSONB)
    policy_snapshot_ref_json: Mapped[dict[str, object]] = mapped_column(JSONB)
    locale: Mapped[str] = mapped_column(String(35))
    timezone: Mapped[str] = mapped_column(String(64))
    contract_version: Mapped[str] = mapped_column(String(64))
    snapshot_digest: Mapped[str] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskAttachmentBindingRecord(Base):
    __tablename__ = "task_attachment_binding"
    __table_args__ = (
        UniqueConstraint("task_id", "artifact_id"),
        UniqueConstraint("task_id", "ordinal"),
        CheckConstraint("ordinal BETWEEN 1 AND 20", name="ordinal_range"),
        CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'", name="source_sha256_lower_hex"),
        CheckConstraint("row_version >= 1", name="row_version_positive"),
        CheckConstraint("state IN ('ACTIVE','EXPIRED','DELETED')", name="state_allowed"),
        {"schema": "workflow"},
    )

    binding_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT")
    )
    artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    owner_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.user_account.id", ondelete="RESTRICT")
    )
    introduced_by_message_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.conversation_message.id", ondelete="RESTRICT")
    )
    source_sha256: Mapped[str] = mapped_column(CHAR(64))
    # content.preflight_run尚未进入已批准表投影。
    preflight_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    detected_media_type: Mapped[str] = mapped_column(String(128))
    ordinal: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    task_terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class TaskExplicitConstraintRecord(Base):
    __tablename__ = "task_explicit_constraint"
    __table_args__ = (
        CheckConstraint(
            "source IN ('POLICY','UI_FIELD','QUERY_SPAN','PRIOR_TASK',"
            "'USER_PREFERENCE','SYSTEM_DEFAULT')",
            name="source_allowed",
        ),
        CheckConstraint("polarity IN ('REQUIRE','FORBID')", name="polarity_allowed"),
        CheckConstraint(
            "resolution_status IN ('EFFECTIVE','SHADOWED','CONFLICTED')",
            name="resolution_status_allowed",
        ),
        CheckConstraint(
            "(source = 'UI_FIELD' AND source_field IS NOT NULL AND "
            "source_span_start IS NULL AND source_span_end IS NULL) OR "
            "(source = 'QUERY_SPAN' AND source_field IS NULL AND source_span_start >= 0 "
            "AND source_span_end > source_span_start) OR "
            "(source NOT IN ('UI_FIELD','QUERY_SPAN') AND source_span_start IS NULL "
            "AND source_span_end IS NULL)",
            name="source_location_shape",
        ),
        {"schema": "workflow"},
    )

    constraint_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT")
    )
    input_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.task_input_snapshot.input_id", ondelete="RESTRICT"),
    )
    kind: Mapped[str] = mapped_column(String(64))
    normalized_value: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32))
    source_field: Mapped[str | None] = mapped_column(String(128))
    source_span_start: Mapped[int | None] = mapped_column(Integer)
    source_span_end: Mapped[int | None] = mapped_column(Integer)
    polarity: Mapped[str] = mapped_column(String(16))
    parser_version: Mapped[str] = mapped_column(String(64))
    resolution_status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskIntentParseRecord(Base):
    __tablename__ = "task_intent_parse"
    __table_args__ = (
        UniqueConstraint("task_id", "input_id", "parser_version"),
        CheckConstraint(
            "constraint_set_digest ~ '^[0-9a-f]{64}$'", name="constraint_digest_lower_hex"
        ),
        CheckConstraint(
            "status IN ('CONSTRAINT_CONFLICT','CLASSIFIED','UNMATCHED','INVALID_OUTPUT',"
            "'DEGRADED_TO_SUPERVISOR')",
            name="status_allowed",
        ),
        {"schema": "workflow"},
    )

    parse_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT")
    )
    input_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.task_input_snapshot.input_id", ondelete="RESTRICT"),
    )
    parser_version: Mapped[str] = mapped_column(String(64))
    constraint_set_digest: Mapped[str] = mapped_column(CHAR(64))
    classification_schema_version: Mapped[str] = mapped_column(String(64))
    classification_artifact_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.artifact.artifact_id", ondelete="RESTRICT")
    )
    fixed_intent_candidate: Mapped[str | None] = mapped_column(String(64))
    required_slots_json: Mapped[list[object]] = mapped_column(JSONB)
    missing_slots_json: Mapped[list[object]] = mapped_column(JSONB)
    ambiguity_flags_json: Mapped[list[object]] = mapped_column(JSONB)
    evidence_spans_json: Mapped[list[object]] = mapped_column(JSONB)
    model_profile_ref_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FixedWorkflowAdmissionRecord(Base):
    __tablename__ = "fixed_workflow_admission"
    __table_args__ = (
        UniqueConstraint("task_id", "input_id", "parse_id", "profile_version"),
        CheckConstraint(
            "decision IN ('ADMIT_FIXED_WORKFLOW','SUPERVISOR')", name="decision_allowed"
        ),
        CheckConstraint("profile_digest ~ '^[0-9a-f]{64}$'", name="profile_digest_lower_hex"),
        CheckConstraint(
            "anchor_grammar_digest ~ '^[0-9a-f]{64}$'", name="grammar_digest_lower_hex"
        ),
        CheckConstraint(
            "slot_resolver_digest ~ '^[0-9a-f]{64}$'", name="resolver_digest_lower_hex"
        ),
        CheckConstraint(
            "language_alias_map_digest ~ '^[0-9a-f]{64}$'", name="alias_digest_lower_hex"
        ),
        CheckConstraint(
            "(decision = 'ADMIT_FIXED_WORKFLOW' AND candidate IS NOT NULL AND "
            "workflow_id IS NOT NULL AND workflow_version IS NOT NULL AND "
            "output_contract_ref IS NOT NULL AND output_contract_digest IS NOT NULL AND "
            "renderer_profile IS NOT NULL AND renderer_digest IS NOT NULL) OR "
            "(decision = 'SUPERVISOR' AND workflow_id IS NULL AND workflow_version IS NULL "
            "AND output_contract_ref IS NULL AND output_contract_digest IS NULL "
            "AND renderer_profile IS NULL AND renderer_digest IS NULL)",
            name="decision_branch_shape",
        ),
        {"schema": "workflow"},
    )

    admission_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT")
    )
    input_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.task_input_snapshot.input_id", ondelete="RESTRICT"),
    )
    parse_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.task_intent_parse.parse_id", ondelete="RESTRICT")
    )
    profile_id: Mapped[str] = mapped_column(String(128))
    profile_version: Mapped[str] = mapped_column(String(64))
    profile_digest: Mapped[str] = mapped_column(CHAR(64))
    anchor_grammar_version: Mapped[str] = mapped_column(String(64))
    anchor_grammar_digest: Mapped[str] = mapped_column(CHAR(64))
    normalization_profile: Mapped[str] = mapped_column(String(128))
    slot_resolver_profile: Mapped[str] = mapped_column(String(128))
    slot_resolver_digest: Mapped[str] = mapped_column(CHAR(64))
    language_alias_map_version: Mapped[str] = mapped_column(String(64))
    language_alias_map_digest: Mapped[str] = mapped_column(CHAR(64))
    candidate: Mapped[str | None] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(32))
    check_results_json: Mapped[list[object]] = mapped_column(JSONB)
    reason_codes_json: Mapped[list[object]] = mapped_column(JSONB)
    anchor_matches_json: Mapped[list[object]] = mapped_column(JSONB)
    slot_resolution_json: Mapped[dict[str, object]] = mapped_column(JSONB)
    workflow_id: Mapped[str | None] = mapped_column(String(128))
    workflow_version: Mapped[str | None] = mapped_column(String(64))
    output_contract_ref: Mapped[str | None] = mapped_column(String(256))
    output_contract_digest: Mapped[str | None] = mapped_column(CHAR(64))
    renderer_profile: Mapped[str | None] = mapped_column(String(128))
    renderer_digest: Mapped[str | None] = mapped_column(CHAR(64))
    classification_schema_version: Mapped[str] = mapped_column(String(64))
    model_profile_version: Mapped[str] = mapped_column(String(64))
    prompt_profile_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskEventRecord(Base):
    __tablename__ = "task_event"
    __table_args__ = (
        PrimaryKeyConstraint("task_id", "sequence"),
        UniqueConstraint("id"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint(
            "business_version IS NULL OR business_version >= 1", name="business_version_positive"
        ),
        CheckConstraint("observation_expected_count >= 0", name="observation_count_nonnegative"),
        CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="payload_digest_lower_hex"),
        CheckConstraint(
            "(business_ref_type IS NULL AND business_ref_id IS NULL "
            "AND business_version IS NULL) OR "
            "(business_ref_type IS NOT NULL AND business_ref_id IS NOT NULL "
            "AND business_version IS NOT NULL)",
            name="business_ref_all_or_none",
        ),
        CheckConstraint(
            "NOT workflow_relevant OR (business_ref_type IS NOT NULL AND "
            "business_ref_id IS NOT NULL AND business_version IS NOT NULL)",
            name="workflow_event_has_business_ref",
        ),
        Index(
            "uq_task_event_business_version",
            "task_id",
            "business_ref_type",
            "business_ref_id",
            "business_version",
            unique=True,
            postgresql_where=text("business_ref_id IS NOT NULL"),
        ),
        Index("ix_task_event_workflow_sequence", "task_id", "workflow_relevant", "sequence"),
        {"schema": "workflow"},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(64))
    event_schema_version: Mapped[str] = mapped_column(String(64))
    workflow_relevant: Mapped[bool] = mapped_column(Boolean)
    business_ref_type: Mapped[str | None] = mapped_column(String(64))
    business_ref_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    business_version: Mapped[int | None] = mapped_column(BigInteger)
    observation_projection_profile_version: Mapped[str | None] = mapped_column(String(64))
    observation_expected_count: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[dict[str, object]] = mapped_column(JSONB)
    payload_digest: Mapped[str] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboxMessageRecord(Base):
    __tablename__ = "outbox_message"
    __table_args__ = (
        UniqueConstraint("event_id", "destination"),
        CheckConstraint("aggregate_version >= 1", name="aggregate_version_positive"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint("row_version >= 1", name="row_version_positive"),
        CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="payload_digest_lower_hex"),
        CheckConstraint(
            "destination IN ('MQ','TEMPORAL_START','TEMPORAL_SIGNAL','TEMPORAL_CANCEL')",
            name="destination_allowed",
        ),
        CheckConstraint(
            "publish_status IN ('PENDING','PUBLISHING','PUBLISHED','DEAD')",
            name="publish_status_allowed",
        ),
        CheckConstraint(
            "(publish_status = 'PUBLISHING' AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
            "AND published_at IS NULL) OR "
            "(publish_status = 'PUBLISHED' AND published_at IS NOT NULL) OR "
            "(publish_status IN ('PENDING','DEAD') AND claimed_by IS NULL AND claimed_at IS NULL "
            "AND published_at IS NULL)",
            name="publish_state_shape",
        ),
        Index("ix_outbox_claim", "publish_status", "available_at", "id"),
        {"schema": "integration"},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.task_event.id", ondelete="RESTRICT")
    )
    aggregate_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    aggregate_version: Mapped[int] = mapped_column(BigInteger)
    destination: Mapped[str] = mapped_column(String(32))
    schema_version: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, object]] = mapped_column(JSONB)
    payload_digest: Mapped[str] = mapped_column(CHAR(64))
    publish_status: Mapped[str] = mapped_column(String(16))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_record"
    __table_args__ = (
        UniqueConstraint("scope", "actor_id", "key_digest"),
        CheckConstraint("key_digest ~ '^[0-9a-f]{64}$'", name="key_digest_lower_hex"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="request_hash_lower_hex"),
        CheckConstraint(
            "response_digest IS NULL OR response_digest ~ '^[0-9a-f]{64}$'",
            name="response_digest_lower_hex",
        ),
        CheckConstraint("status IN ('PROCESSING','COMPLETED')", name="status_allowed"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint(
            "(status = 'PROCESSING' AND resource_type IS NULL AND resource_id IS NULL "
            "AND response_code IS NULL AND response_body_json IS NULL "
            "AND response_digest IS NULL) OR "
            "(status = 'COMPLETED' AND resource_type IS NOT NULL AND resource_id IS NOT NULL "
            "AND response_code IS NOT NULL AND response_body_json IS NOT NULL "
            "AND response_digest IS NOT NULL)",
            name="status_result_shape",
        ),
        {"schema": "integration"},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.user_account.id", ondelete="RESTRICT")
    )
    key_digest: Mapped[str] = mapped_column(CHAR(64))
    key_hash_version: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(CHAR(64))
    status: Mapped[str] = mapped_column(String(16))
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    response_code: Mapped[int | None] = mapped_column(Integer)
    response_body_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    response_digest: Mapped[str | None] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
