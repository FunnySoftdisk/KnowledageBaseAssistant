"""DEV-01A首批跨模块基础表；不包含Task事务表。"""

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
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class OrganizationRecord(Base):
    __tablename__ = "organization"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','SUSPENDED')", name="status_allowed"),
        CheckConstraint("version >= 1", name="version_positive"),
        {"schema": "iam"},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserAccountRecord(Base):
    __tablename__ = "user_account"
    __table_args__ = (
        UniqueConstraint("normalized_username"),
        CheckConstraint(
            "status IN ('PENDING_FIRST_LOGIN','ACTIVE','DISABLED')", name="status_allowed"
        ),
        CheckConstraint("failed_login_count >= 0", name="failed_login_count_nonnegative"),
        CheckConstraint("credential_version >= 1", name="credential_version_positive"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_user_account_status", "status"),
        {"schema": "iam"},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.organization.id", ondelete="RESTRICT")
    )
    username: Mapped[str] = mapped_column(String(64))
    normalized_username: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    must_change_password: Mapped[bool] = mapped_column(Boolean)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credential_version: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ConversationRecord(Base):
    __tablename__ = "conversation"
    __table_args__ = (
        CheckConstraint(
            "memory_policy IN ('OFF','CONVERSATION','USER_PRIVATE_ALLOWED')",
            name="memory_policy_allowed",
        ),
        CheckConstraint("conversation_memory_version >= 0", name="memory_version_nonnegative"),
        Index("ix_conversation_owner_updated", "owner_id", "updated_at", "id"),
        {"schema": "workflow"},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.user_account.id", ondelete="RESTRICT")
    )
    organization_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.organization.id", ondelete="RESTRICT")
    )
    title: Mapped[str] = mapped_column(String(256))
    mode: Mapped[str] = mapped_column(String(32))
    conversation_memory_version: Mapped[int] = mapped_column(BigInteger, default=0)
    memory_policy: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ArtifactRecord(Base):
    __tablename__ = "artifact"
    __table_args__ = (
        UniqueConstraint("object_key"),
        Index(
            "uq_artifact_origin_upload_not_null",
            "origin_upload_id",
            unique=True,
            postgresql_where="origin_upload_id IS NOT NULL",
        ),
        CheckConstraint("size_bytes >= 0", name="size_bytes_nonnegative"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_lower_hex"),
        CheckConstraint("schema_digest ~ '^[0-9a-f]{64}$'", name="schema_digest_lower_hex"),
        CheckConstraint("row_version >= 1", name="row_version_positive"),
        CheckConstraint(
            "state IN ('STAGED','QUARANTINED','PREFLIGHTING','AVAILABLE','REJECTED',"
            "'UNSUPPORTED','SCAN_UNAVAILABLE','DELETING','DELETED')",
            name="state_allowed",
        ),
        CheckConstraint(
            "purpose <> 'TASK_ATTACHMENT' OR "
            "(owner_id IS NOT NULL AND preflight_run_id IS NOT NULL)",
            name="task_attachment_requires_owner_and_preflight",
        ),
        {"schema": "content"},
    )

    artifact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.organization.id", ondelete="RESTRICT")
    )
    owner_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.user_account.id", ondelete="RESTRICT")
    )
    # 两项FK在Upload/Preflight表进入同一迁移包时补为有效约束。
    origin_upload_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    purpose: Mapped[str] = mapped_column(String(32))
    artifact_type: Mapped[str] = mapped_column(String(128))
    storage_backend: Mapped[str] = mapped_column(String(32))
    object_key: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(CHAR(64))
    declared_media_type: Mapped[str | None] = mapped_column(String(128))
    detected_media_type: Mapped[str] = mapped_column(String(128))
    schema_id: Mapped[str] = mapped_column(String(128))
    schema_version: Mapped[str] = mapped_column(String(64))
    schema_digest: Mapped[str] = mapped_column(CHAR(64))
    data_labels_json: Mapped[dict[str, object]] = mapped_column(JSONB)
    encryption_profile: Mapped[str] = mapped_column(String(64))
    preflight_run_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    state: Mapped[str] = mapped_column(String(24))
    retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    legal_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_by_actor_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.user_account.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PolicySnapshotRecord(Base):
    __tablename__ = "policy_snapshot"
    __table_args__ = (
        UniqueConstraint("organization_id", "actor_id", "policy_revision", "policy_digest"),
        CheckConstraint("policy_revision >= 1", name="policy_revision_positive"),
        CheckConstraint("policy_bundle_digest ~ '^[0-9a-f]{64}$'", name="bundle_digest_lower_hex"),
        CheckConstraint("effective_scope_digest ~ '^[0-9a-f]{64}$'", name="scope_digest_lower_hex"),
        CheckConstraint("policy_digest ~ '^[0-9a-f]{64}$'", name="policy_digest_lower_hex"),
        {"schema": "policy"},
    )

    policy_snapshot_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.organization.id", ondelete="RESTRICT")
    )
    actor_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("iam.user_account.id", ondelete="RESTRICT")
    )
    policy_revision: Mapped[int] = mapped_column(BigInteger)
    policy_bundle_id: Mapped[str] = mapped_column(String(128))
    policy_bundle_version: Mapped[str] = mapped_column(String(64))
    policy_bundle_digest: Mapped[str] = mapped_column(CHAR(64))
    effective_scope_digest: Mapped[str] = mapped_column(CHAR(64))
    policy_digest: Mapped[str] = mapped_column(CHAR(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
