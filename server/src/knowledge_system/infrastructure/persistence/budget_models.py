"""DEV-04C `workflow.budget_reservation`/`workflow.budget_ledger_entry`预算预留与账本表。

依据数据库设计§9.12：`budget_reservation`保存同一逻辑外调唯一的五维预留上限与实耗、状态与
`row_version`；`budget_ledger_entry`只追加带符号五维增量分录，`entry_kind`限定六种且每维总账
守恒。预算账户（`budget_account`）、Branch/Delegation/Action Attempt/Capability Invocation与
事件表尚未冻结，`account_id`/`branch_id`/`delegation_id`/`action_attempt_id`/`invocation_id`/
`caused_by_event_id`保持FK_DEFERRED裸UUID列；`task`/`task_attempt`/`model_call`已冻结，落地
真实外键。`model_call.budget_reservation_id`的逆向边（预留→调用的反向引用）为避免跨迁移环形
外键，暂维持FK_DEFERRED，待预留删除策略冻结后闭合。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CHAR,
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
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class BudgetReservationRecord(Base):
    __tablename__ = "budget_reservation"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RESERVED','CONSUMING','SETTLED','RELEASED','EXPIRED','RECONCILING')",
            name="status_allowed",
        ),
        CheckConstraint(
            "reserved_model_call_count >= 0 AND reserved_input_tokens >= 0 AND "
            "reserved_output_tokens >= 0 AND reserved_tool_count >= 0 AND "
            "reserved_cost_amount >= 0",
            name="reserved_nonnegative",
        ),
        CheckConstraint(
            "settled_model_call_count >= 0 AND settled_input_tokens >= 0 AND "
            "settled_output_tokens >= 0 AND settled_tool_count >= 0 AND "
            "settled_cost_amount >= 0",
            name="settled_nonnegative",
        ),
        CheckConstraint(
            "settled_model_call_count <= reserved_model_call_count AND "
            "settled_input_tokens <= reserved_input_tokens AND "
            "settled_output_tokens <= reserved_output_tokens AND "
            "settled_tool_count <= reserved_tool_count AND "
            "settled_cost_amount <= reserved_cost_amount",
            name="settled_le_reserved",
        ),
        CheckConstraint(
            "reservation_digest ~ '^[0-9a-f]{64}$'", name="reservation_digest_lower_hex"
        ),
        CheckConstraint("reservation_version >= 1", name="reservation_version_positive"),
        CheckConstraint("row_version >= 1", name="row_version_positive"),
        Index(
            "uq_budget_reservation_model_call",
            "model_call_id",
            unique=True,
            postgresql_where=text("model_call_id IS NOT NULL"),
        ),
        Index(
            "uq_budget_reservation_invocation",
            "invocation_id",
            unique=True,
            postgresql_where=text("invocation_id IS NOT NULL"),
        ),
        Index("ix_budget_reservation_task_created", "task_id", "created_at"),
        Index("ix_budget_reservation_status", "status"),
        {"schema": "workflow"},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT")
    )
    # branch/delegation/action_attempt/invocation/account表尚未冻结。
    branch_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    delegation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    task_attempt_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.task_attempt.id", ondelete="RESTRICT")
    )
    model_call_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.model_call.id", ondelete="RESTRICT")
    )
    action_attempt_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    invocation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    account_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    reservation_version: Mapped[int] = mapped_column(Integer)
    reserved_model_call_count: Mapped[int] = mapped_column(Integer)
    reserved_input_tokens: Mapped[int] = mapped_column(Integer)
    reserved_output_tokens: Mapped[int] = mapped_column(Integer)
    reserved_tool_count: Mapped[int] = mapped_column(Integer)
    reserved_cost_amount: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    settled_model_call_count: Mapped[int] = mapped_column(Integer)
    settled_input_tokens: Mapped[int] = mapped_column(Integer)
    settled_output_tokens: Mapped[int] = mapped_column(Integer)
    settled_tool_count: Mapped[int] = mapped_column(Integer)
    settled_cost_amount: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    reservation_digest: Mapped[str] = mapped_column(CHAR(64))
    status: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class BudgetLedgerEntryRecord(Base):
    __tablename__ = "budget_ledger_entry"
    __table_args__ = (
        CheckConstraint(
            "entry_kind IN ('ALLOCATE','RESERVE','SETTLE','RELEASE','RETURN',"
            "'WORST_CASE_SETTLE')",
            name="entry_kind_allowed",
        ),
        CheckConstraint("currency <> ''", name="currency_nonempty"),
        Index("ix_budget_ledger_entry_reservation_created", "reservation_id", "created_at"),
        Index("ix_budget_ledger_entry_task", "task_id"),
        {"schema": "workflow"},
    )

    entry_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT")
    )
    # account/event表尚未冻结。
    account_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    reservation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.budget_reservation.id", ondelete="RESTRICT")
    )
    entry_kind: Mapped[str] = mapped_column(String(32))
    model_call_count_delta: Mapped[int] = mapped_column(Integer)
    input_tokens_delta: Mapped[int] = mapped_column(Integer)
    output_tokens_delta: Mapped[int] = mapped_column(Integer)
    tool_count_delta: Mapped[int] = mapped_column(Integer)
    cost_amount_delta: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    currency: Mapped[str] = mapped_column(String(8))
    rate_card_version: Mapped[str | None] = mapped_column(String(64))
    exchange_rate_snapshot_version: Mapped[str | None] = mapped_column(String(64))
    caused_by_event_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
