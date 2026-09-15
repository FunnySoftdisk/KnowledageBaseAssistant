"""CORE-09～16：任务域基础引用。引用携带身份/完整性，不授予权限。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Sha256Hex = Annotated[
    str,
    StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]
ShortVersion = Annotated[str, StringConstraints(min_length=1, max_length=64)]
StableKey = Annotated[str, StringConstraints(min_length=1, max_length=128)]


def canonical_json_sha256(value: Any) -> str:
    """只接受RFC 8785可表达值；调用方负责先转为JSON模式。"""

    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


class StrictContractV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ArtifactRefV1(StrictContractV1):
    artifact_id: UUID
    artifact_type: StableKey
    sha256: Sha256Hex
    schema_id: StableKey
    schema_version: ShortVersion


class SchemaRefV1(StrictContractV1):
    schema_id: StableKey
    schema_version: ShortVersion
    schema_digest: Sha256Hex


class CapabilityRefV1(StrictContractV1):
    capability_id: StableKey
    capability_version: ShortVersion
    integrity_digest: Sha256Hex


class TaskAttachmentBindingRefV1(StrictContractV1):
    binding_id: UUID
    task_id: UUID
    artifact_ref: ArtifactRefV1
    ordinal: int = Field(ge=1, le=20)


class ResourceDomainV1(StrEnum):
    TASK_ATTACHMENT = "TASK_ATTACHMENT"
    TEAM_KNOWLEDGE = "TEAM_KNOWLEDGE"
    PERSONAL_KNOWLEDGE = "PERSONAL_KNOWLEDGE"
    TASK_RESULT = "TASK_RESULT"


class ResourceRefV1(StrictContractV1):
    source_domain: ResourceDomainV1
    resource_id: StableKey
    version_id: StableKey


class ConstraintRefV1(StrictContractV1):
    constraint_id: UUID
    input_id: UUID
    parser_version: ShortVersion


class PolicySnapshotRefV1(StrictContractV1):
    policy_snapshot_id: UUID
    policy_revision: int = Field(ge=1)
    policy_digest: Sha256Hex
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTC_DATETIME_REQUIRED")
        return value.astimezone(UTC)
