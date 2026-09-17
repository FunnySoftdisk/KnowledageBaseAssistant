"""`audit.audit_event`追加表的SQLAlchemy映射；应用只INSERT，不提供UPDATE/DELETE路径。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CHAR, CheckConstraint, DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AuditEventRecord(Base):
    __tablename__ = "audit_event"
    __table_args__ = (
        CheckConstraint(
            "result IN ('SUCCESS','FAILURE','DENIED')", name="result_allowed"
        ),
        CheckConstraint("event_hash ~ '^[0-9a-f]{64}$'", name="event_hash_lower_hex"),
        CheckConstraint(
            "previous_hash IS NULL OR previous_hash ~ '^[0-9a-f]{64}$'",
            name="previous_hash_lower_hex",
        ),
        CheckConstraint(
            "before_digest IS NULL OR before_digest ~ '^[0-9a-f]{64}$'",
            name="before_digest_lower_hex",
        ),
        CheckConstraint(
            "after_digest IS NULL OR after_digest ~ '^[0-9a-f]{64}$'",
            name="after_digest_lower_hex",
        ),
        Index("ix_audit_event_occurred_at", "occurred_at"),
        Index("ix_audit_event_actor_id", "actor_id"),
        Index("ix_audit_event_resource", "resource_type", "resource_id"),
        {"schema": "audit"},
    )

    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    actor_role_snapshot: Mapped[list[str] | None] = mapped_column(JSONB)
    session_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    source_ip: Mapped[str | None] = mapped_column(String(64))
    device_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(128))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    result: Mapped[str] = mapped_column(String(32))
    reason_code: Mapped[str | None] = mapped_column(String(64))
    before_digest: Mapped[str | None] = mapped_column(CHAR(64))
    after_digest: Mapped[str | None] = mapped_column(CHAR(64))
    details_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    trace_id: Mapped[str] = mapped_column(String(64))
    previous_hash: Mapped[str | None] = mapped_column(CHAR(64))
    event_hash: Mapped[str] = mapped_column(CHAR(64))


class AuditChainHeadRecord(Base):
    """Hash链当前链头；纳入metadata防止后续迁移误判为应删除表。"""

    __tablename__ = "audit_chain_head"
    __table_args__ = {"schema": "audit"}

    chain_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    head_hash: Mapped[str | None] = mapped_column(CHAR(64))
