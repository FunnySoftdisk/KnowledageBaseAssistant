"""SQLAlchemy mapping for the one-shot model egress permit."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class EgressPermitRecord(Base):
    __tablename__ = "model_egress_permit"
    __table_args__ = (
        CheckConstraint(
            "provider = 'alibaba_bailian'",
            name="provider_allowed",
        ),
        CheckConstraint(
            "status IN ('READY','ADMITTED','SECRET_READ','REVOKED','EXPIRED')",
            name="status_allowed",
        ),
        CheckConstraint(
            "parameters_digest ~ '^[0-9a-f]{64}$' AND "
            "(schema_digest IS NULL OR schema_digest ~ '^[0-9a-f]{64}$') AND "
            "input_digest ~ '^[0-9a-f]{64}$'",
            name="digests_lower_hex",
        ),
        CheckConstraint("secret_version >= 1", name="secret_version_positive"),
        CheckConstraint("authorization_version >= 1", name="authorization_version_positive"),
        CheckConstraint("execution_generation >= 1", name="execution_generation_positive"),
        CheckConstraint("cancellation_generation >= 0", name="cancellation_generation_nonnegative"),
        CheckConstraint("row_version >= 1", name="row_version_positive"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint(
            "(status = 'READY' AND admitted_at IS NULL AND secret_read_at IS NULL) OR "
            "(status = 'ADMITTED' AND admitted_at IS NOT NULL AND secret_read_at IS NULL) OR "
            "(status = 'SECRET_READ' AND admitted_at IS NOT NULL "
            "AND secret_read_at IS NOT NULL) OR "
            "(status IN ('REVOKED','EXPIRED') AND secret_read_at IS NULL)",
            name="lifecycle_timestamps",
        ),
        {"schema": "workflow"},
    )

    model_call_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.model_call.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    route_ref: Mapped[str] = mapped_column(String(256))
    provider: Mapped[str] = mapped_column(String(64))
    provider_model: Mapped[str] = mapped_column(String(192))
    secret_ref: Mapped[str] = mapped_column(String(256))
    secret_version: Mapped[int] = mapped_column(Integer)
    model_revision: Mapped[str] = mapped_column(String(192))
    parameters_digest: Mapped[str] = mapped_column(CHAR(64))
    schema_digest: Mapped[str | None] = mapped_column(CHAR(64))
    input_digest: Mapped[str] = mapped_column(CHAR(64))
    authorization_version: Mapped[int] = mapped_column(BigInteger)
    execution_generation: Mapped[int] = mapped_column(BigInteger)
    cancellation_generation: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    admitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    secret_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


__all__ = ["EgressPermitRecord"]
