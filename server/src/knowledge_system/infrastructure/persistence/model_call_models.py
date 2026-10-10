"""DEV-04B `workflow.model_call`模型调用账本表。

`workflow.model_call`是模型调用业务账本唯一权威（模块设计§12、数据库§10.4）：保存稳定
`model_call_id`、批准/实际端点与模型、Context Pack引用、Token Accounting与Usage、预算预留
引用、耗时、状态与稳定错误码。Token Accounting按C03的EXACT/ESTIMATED_DEV分支落地为判别列
`token_accounting_kind`+互斥分支列（估算分支不伪造tokenizer/template精确字段）；Usage按
`usage_source`三态落地（UNKNOWN不携带Token/费用）。尚未冻结FK的引用（Capability Grant、
Endpoint Registry、Branch/Delegation、Budget Reservation、Action Attempt）保持FK_DEFERRED
裸UUID列，待对应表投影批准后在后续迁移闭合。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ModelCallRecord(Base):
    __tablename__ = "model_call"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACCEPTED','SENDING','STREAMING','COMPLETED','FAILED','CANCELLED',"
            "'UNKNOWN')",
            name="status_allowed",
        ),
        CheckConstraint(
            "token_accounting_kind IN ('EXACT','ESTIMATED_DEV')",
            name="token_accounting_kind_allowed",
        ),
        CheckConstraint(
            "usage_source IN ('PROVIDER','WORST_CASE_RESERVATION','UNKNOWN')",
            name="usage_source_allowed",
        ),
        CheckConstraint(
            "data_scope IN ('PUBLIC_DEVELOPMENT','APPROVED_EXTERNAL_TRIAL',"
            "'INTERNAL_PRODUCTION','PUBLIC_EVALUATION')",
            name="data_scope_allowed",
        ),
        CheckConstraint(
            "(token_accounting_kind = 'EXACT' AND tokenizer_revision IS NOT NULL AND "
            "chat_template_hash IS NOT NULL AND exact_input_tokens IS NOT NULL AND "
            "estimator_revision IS NULL AND serialized_request_bytes IS NULL AND "
            "protocol_overhead_tokens IS NULL AND estimated_input_tokens IS NULL) OR "
            "(token_accounting_kind = 'ESTIMATED_DEV' AND estimator_revision IS NOT NULL AND "
            "serialized_request_bytes IS NOT NULL AND protocol_overhead_tokens IS NOT NULL AND "
            "estimated_input_tokens IS NOT NULL AND tokenizer_revision IS NULL AND "
            "chat_template_hash IS NULL AND exact_input_tokens IS NULL)",
            name="token_accounting_branch",
        ),
        CheckConstraint(
            "(usage_source = 'UNKNOWN' AND input_tokens IS NULL AND output_tokens IS NULL) OR "
            "(usage_source <> 'UNKNOWN' AND input_tokens IS NOT NULL AND output_tokens IS NOT "
            "NULL)",
            name="usage_branch",
        ),
        CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$' AND "
            "(response_hash IS NULL OR response_hash ~ '^[0-9a-f]{64}$') AND "
            "(chat_template_hash IS NULL OR chat_template_hash ~ '^[0-9a-f]{64}$')",
            name="digests_lower_hex",
        ),
        CheckConstraint(
            "conversation_memory_version >= 0 AND user_private_memory_version >= 0 AND "
            "task_memory_version >= 0",
            name="memory_versions_nonnegative",
        ),
        CheckConstraint(
            "(exact_input_tokens IS NULL OR exact_input_tokens >= 0) AND "
            "(estimated_input_tokens IS NULL OR estimated_input_tokens >= 0) AND "
            "(serialized_request_bytes IS NULL OR serialized_request_bytes >= 0) AND "
            "(protocol_overhead_tokens IS NULL OR protocol_overhead_tokens >= 0) AND "
            "(input_tokens IS NULL OR input_tokens >= 0) AND "
            "(output_tokens IS NULL OR output_tokens >= 0) AND "
            "(queue_ms IS NULL OR queue_ms >= 0) AND (ttft_ms IS NULL OR ttft_ms >= 0) AND "
            "(duration_ms IS NULL OR duration_ms >= 0)",
            name="counts_nonnegative",
        ),
        CheckConstraint("row_version >= 1", name="row_version_positive"),
        Index(
            "uq_model_call_provider_request_id",
            "provider_request_id",
            unique=True,
            postgresql_where=text("provider_request_id IS NOT NULL"),
        ),
        Index("ix_model_call_task_created", "task_id", "created_at"),
        Index("ix_model_call_status", "status"),
        {"schema": "workflow"},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT"),
    )
    attempt_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.task_attempt.id", ondelete="RESTRICT"),
    )
    # action_attempt/branch/delegation/grant/endpoint/budget_reservation表尚未冻结。
    action_attempt_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    branch_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    delegation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    execution_generation: Mapped[int | None] = mapped_column(BigInteger)
    cancellation_generation: Mapped[int | None] = mapped_column(BigInteger)
    approved_endpoint_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    actual_endpoint_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    capability_alias: Mapped[str] = mapped_column(String(64))
    approved_model_id: Mapped[str] = mapped_column(String(128))
    approved_model_revision: Mapped[str] = mapped_column(String(128))
    actual_provider: Mapped[str | None] = mapped_column(String(64))
    actual_model_id: Mapped[str | None] = mapped_column(String(128))
    actual_model_revision: Mapped[str | None] = mapped_column(String(128))
    bifrost_request_id: Mapped[str | None] = mapped_column(String(128))
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(64))
    tool_schema_versions: Mapped[dict[str, str]] = mapped_column(JSONB)
    context_pack_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.context_pack.id", ondelete="RESTRICT")
    )
    context_policy_version: Mapped[str] = mapped_column(String(64))
    authorization_version: Mapped[int] = mapped_column(BigInteger)
    grant_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    runtime_version: Mapped[str] = mapped_column(String(64))
    plan_version: Mapped[int | None] = mapped_column(Integer)
    agent_manifest_version: Mapped[str] = mapped_column(String(64))
    conversation_memory_version: Mapped[int] = mapped_column(BigInteger)
    user_private_memory_version: Mapped[int] = mapped_column(BigInteger)
    task_memory_version: Mapped[int] = mapped_column(BigInteger)
    token_accounting_kind: Mapped[str] = mapped_column(String(16))
    tokenizer_revision: Mapped[str | None] = mapped_column(String(128))
    chat_template_hash: Mapped[str | None] = mapped_column(CHAR(64))
    exact_input_tokens: Mapped[int | None] = mapped_column(Integer)
    estimator_revision: Mapped[str | None] = mapped_column(String(128))
    serialized_request_bytes: Mapped[int | None] = mapped_column(Integer)
    protocol_overhead_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_schema_id: Mapped[str | None] = mapped_column(String(128))
    output_schema_version: Mapped[str | None] = mapped_column(String(64))
    data_scope: Mapped[str] = mapped_column(String(32))
    request_hash: Mapped[str] = mapped_column(CHAR(64))
    response_hash: Mapped[str | None] = mapped_column(CHAR(64))
    usage_source: Mapped[str] = mapped_column(String(24))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    cost_currency: Mapped[str | None] = mapped_column(String(8))
    budget_reservation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    rate_card_version: Mapped[str | None] = mapped_column(String(64))
    exchange_rate_snapshot_version: Mapped[str | None] = mapped_column(String(64))
    queue_ms: Mapped[int | None] = mapped_column(Integer)
    ttft_ms: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
