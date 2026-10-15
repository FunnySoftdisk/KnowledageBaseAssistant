"""`memory.memory_record` / `memory.memory_forget_tombstone` 权威 Store 映射（ADR-0004§4）。

`memory_record` 是追加式不可变 Revision 逻辑记录表：纠正/冲突创建新行（`supersedes_id` 指向前一
Revision），旧 Revision 状态转为 `SUPERSEDED`；`provenance_json` 内联不可变来源引用。遗忘先落
`memory_forget_tombstone` 再把记录置为 `RETRACTED`。Embedding Projection（pgvector）留待召回切片。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

_SCOPE_VALUES = "'TASK','CONVERSATION','USER_PRIVATE'"
_KIND_VALUES = (
    "'GOAL','CONSTRAINT','DEFINITION','DECISION','PREFERENCE','ENTITY_BINDING',"
    "'OPEN_QUESTION','RESOLUTION','SUPPORTED_FINDING'"
)
_STATE_VALUES = (
    "'PROPOSED','ACTIVE','SUPERSEDED','RETRACTED','EXPIRED','INVALIDATED','QUARANTINED'"
)
_TRUST_VALUES = "'USER_EXPLICIT','USER_DERIVED','ASSISTANT_DERIVED','EVIDENCE_SUPPORTED'"


class MemoryRecordRecord(Base):
    __tablename__ = "memory_record"
    __table_args__ = (
        CheckConstraint(f"scope IN ({_SCOPE_VALUES})", name="scope_allowed"),
        CheckConstraint(f"kind IN ({_KIND_VALUES})", name="kind_allowed"),
        CheckConstraint(f"state IN ({_STATE_VALUES})", name="state_allowed"),
        CheckConstraint(f"trust IN ({_TRUST_VALUES})", name="trust_allowed"),
        CheckConstraint("importance >= 0 AND importance <= 1", name="importance_range"),
        CheckConstraint("canonical_hash ~ '^[0-9a-f]{64}$'", name="canonical_hash_lower_hex"),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        Index(
            "ix_memory_record_owner_scope_state_hash",
            "owner_id",
            "scope",
            "state",
            "canonical_hash",
        ),
        Index(
            "ix_memory_record_owner_scope_state_key",
            "owner_id",
            "scope",
            "state",
            "canonical_key",
        ),
        {"schema": "memory"},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.organization.id", ondelete="RESTRICT")
    )
    owner_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.user_account.id", ondelete="RESTRICT")
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.conversation.id", ondelete="RESTRICT")
    )
    task_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT")
    )
    scope: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(16))
    trust: Mapped[str] = mapped_column(String(32))
    canonical_key: Mapped[str | None] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    canonical_hash: Mapped[str] = mapped_column(CHAR(64))
    importance: Mapped[float] = mapped_column(Float)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("memory.memory_record.id", ondelete="RESTRICT"),
    )
    conflict_group_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    provenance_json: Mapped[list[object]] = mapped_column(JSONB)
    schema_version: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MemoryForgetTombstoneRecord(Base):
    __tablename__ = "memory_forget_tombstone"
    __table_args__ = (
        CheckConstraint("char_length(reason) BETWEEN 1 AND 256", name="reason_length"),
        {"schema": "memory"},
    )

    tombstone_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    record_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("memory.memory_record.id", ondelete="RESTRICT")
    )
    organization_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.organization.id", ondelete="RESTRICT")
    )
    owner_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.user_account.id", ondelete="RESTRICT")
    )
    reason: Mapped[str] = mapped_column(String(256))
    forgotten_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


__all__ = ["MemoryForgetTombstoneRecord", "MemoryRecordRecord"]
