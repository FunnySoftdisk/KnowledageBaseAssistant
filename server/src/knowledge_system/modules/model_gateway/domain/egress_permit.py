"""Persisted one-shot authorization for the model provider final hop."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from knowledge_system.modules.tasking.domain.core_types import StrictContractV1

from .egress_admission import EgressBodyDigestV1


class EgressPermitStatus(StrEnum):
    READY = "READY"
    ADMITTED = "ADMITTED"
    SECRET_READ = "SECRET_READ"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class EgressPermitV1(StrictContractV1):
    """The PG authority used by both Bridge admission and Broker authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_call_id: UUID
    route_ref: str = Field(min_length=1, max_length=256)
    provider: Literal["alibaba_bailian"]
    provider_model: str = Field(min_length=1, max_length=192)
    secret_ref: str = Field(min_length=1, max_length=256)
    secret_version: int = Field(ge=1)
    body: EgressBodyDigestV1
    authorization_version: int = Field(ge=1)
    execution_generation: int = Field(ge=1)
    cancellation_generation: int = Field(ge=0)
    status: EgressPermitStatus
    expires_at: datetime
    admitted_at: datetime | None = None
    secret_read_at: datetime | None = None
    row_version: int = Field(ge=1)
    created_at: datetime

    @model_validator(mode="after")
    def validate_lifecycle_timestamps(self) -> EgressPermitV1:
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("EGRESS_PERMIT_TIMESTAMP_NAIVE")
        if self.expires_at <= self.created_at:
            raise ValueError("EGRESS_PERMIT_EXPIRY_INVALID")
        if self.status is EgressPermitStatus.READY:
            if self.admitted_at is not None or self.secret_read_at is not None:
                raise ValueError("EGRESS_PERMIT_LIFECYCLE_INVALID")
        elif self.status is EgressPermitStatus.ADMITTED:
            if self.admitted_at is None or self.secret_read_at is not None:
                raise ValueError("EGRESS_PERMIT_LIFECYCLE_INVALID")
        elif self.status is EgressPermitStatus.SECRET_READ:
            if self.admitted_at is None or self.secret_read_at is None:
                raise ValueError("EGRESS_PERMIT_LIFECYCLE_INVALID")
        elif self.secret_read_at is not None:
            raise ValueError("EGRESS_PERMIT_LIFECYCLE_INVALID")
        return self


__all__ = ["EgressPermitStatus", "EgressPermitV1"]
