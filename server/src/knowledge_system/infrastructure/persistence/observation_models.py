"""`workflow.task_observation` / `workflow.observation_disposition` 不可变观察投影映射。

Observation 创建后永不更新；Disposition 由 Controller 追加
（`RESOLVED`/`SUPERSEDED`/`INVALIDATED`）。
"""

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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

_OBSERVATION_SOURCE_KINDS = (
    "'RESULT_ADOPTED','RESULT_NOT_ADOPTED','ACTION_TERMINAL','APPROVAL_TERMINAL',"
    "'EXTERNAL_EFFECT_CHANGED','TASK_INPUT_ADVANCED','RESOURCE_AUTHORIZATION_CHANGED',"
    "'CAPABILITY_AVAILABILITY_CHANGED','BUDGET_THRESHOLD_REACHED','TASK_DEADLINE_REACHED',"
    "'AGENT_ASSESSMENT_REPORTED'"
)


class TaskObservationRecord(Base):
    __tablename__ = "task_observation"
    __table_args__ = (
        CheckConstraint("contract_version = 'task_observation_v1'", name="contract_version_v1"),
        CheckConstraint("source_event_sequence >= 1", name="source_event_sequence_positive"),
        CheckConstraint("source_ordinal >= 0", name="source_ordinal_nonnegative"),
        CheckConstraint(
            "source_plan_version IS NULL OR source_plan_version >= 1",
            name="source_plan_version_positive",
        ),
        CheckConstraint("integrity_digest ~ '^[0-9a-f]{64}$'", name="integrity_digest_lower_hex"),
        CheckConstraint(
            f"source_kind IN ({_OBSERVATION_SOURCE_KINDS})", name="source_kind_allowed"
        ),
        CheckConstraint(
            "observation_class IN ('ADOPTED_RESULT','SYSTEM_FACT','USER_INPUT','AGENT_ASSESSMENT')",
            name="observation_class_allowed",
        ),
        {"schema": "workflow"},
    )

    observation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT"),
    )
    source_event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    source_event_sequence: Mapped[int] = mapped_column(BigInteger)
    source_ordinal: Mapped[int] = mapped_column(Integer)
    source_kind: Mapped[str] = mapped_column(String(64))
    observation_class: Mapped[str] = mapped_column(String(32))
    source_plan_version: Mapped[int | None] = mapped_column(Integer)
    contract_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, object]] = mapped_column(JSONB)
    integrity_digest: Mapped[str] = mapped_column(CHAR(64))


class ObservationDispositionRecord(Base):
    __tablename__ = "observation_disposition"
    __table_args__ = (
        CheckConstraint(
            "contract_version = 'observation_disposition_event_v1'", name="contract_version_v1"
        ),
        CheckConstraint(
            "disposition IN ('RESOLVED','SUPERSEDED','INVALIDATED')", name="disposition_allowed"
        ),
        CheckConstraint(
            "char_length(reason_code) BETWEEN 1 AND 128", name="reason_code_length"
        ),
        {"schema": "workflow"},
    )

    disposition_event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.task_observation.observation_id", ondelete="RESTRICT"),
    )
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow.intelligent_task.id", ondelete="RESTRICT"),
    )
    disposition: Mapped[str] = mapped_column(String(16))
    reason_code: Mapped[str] = mapped_column(String(128))
    caused_by_event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    contract_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


__all__ = ["ObservationDispositionRecord", "TaskObservationRecord"]
