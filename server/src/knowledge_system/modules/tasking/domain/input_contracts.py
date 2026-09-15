"""CORE-17～25：公共任务入口与不可变Input Snapshot。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

import langcodes
from pydantic import Field, StringConstraints, field_validator, model_validator

from .core_types import (
    ArtifactRefV1,
    ConstraintRefV1,
    PolicySnapshotRefV1,
    ResourceRefV1,
    SchemaRefV1,
    Sha256Hex,
    StrictContractV1,
    TaskAttachmentBindingRefV1,
    canonical_json_sha256,
)

ConstraintText = Annotated[str, StringConstraints(min_length=1, max_length=512)]
CREATE_TASK_BODY_MAX_BYTES = 262_144


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("DUPLICATE_JSON_KEY")
        value[key] = item
    return value


class KnowledgeScopeModeV1(StrEnum):
    NONE = "NONE"
    DEFAULT_AUTHORIZED = "DEFAULT_AUTHORIZED"
    EXPLICIT = "EXPLICIT"


class KnowledgeScopeV1(StrictContractV1):
    mode: KnowledgeScopeModeV1
    directory_ids: tuple[UUID, ...] = Field(max_length=64)
    knowledge_version_ids: tuple[UUID, ...] = Field(max_length=64)
    include_personal: bool

    @model_validator(mode="after")
    def validate_branch(self) -> KnowledgeScopeV1:
        if self.mode is not KnowledgeScopeModeV1.EXPLICIT and (
            self.directory_ids or self.knowledge_version_ids or self.include_personal
        ):
            raise ValueError("KNOWLEDGE_SCOPE_BRANCH_INVALID")
        if self.mode is KnowledgeScopeModeV1.EXPLICIT and not (
            self.directory_ids or self.knowledge_version_ids or self.include_personal
        ):
            raise ValueError("EXPLICIT_SCOPE_EMPTY")
        if len(set(self.directory_ids)) != len(self.directory_ids) or len(
            set(self.knowledge_version_ids)
        ) != len(self.knowledge_version_ids):
            raise ValueError("KNOWLEDGE_SCOPE_DUPLICATE")
        return self


class OutputContractV1(StrictContractV1):
    format: Literal["MARKDOWN", "JSON"]
    delivery: Literal["INLINE", "ARTIFACT"]
    language: Annotated[str, StringConstraints(min_length=1, max_length=35)]
    schema_ref: SchemaRefV1 | None
    template_or_skill_ref: ResourceRefV1 | None
    citations: Literal["REQUIRED", "OPTIONAL", "FORBIDDEN"]
    max_output_bytes: int = Field(ge=1, le=1_048_576)

    @field_validator("language")
    @classmethod
    def validate_bcp47_language(cls, value: str) -> str:
        if not langcodes.tag_is_valid(value):
            raise ValueError("BCP47_LANGUAGE_INVALID")
        return value

    @model_validator(mode="after")
    def validate_format(self) -> OutputContractV1:
        if (self.format == "JSON") != (self.schema_ref is not None):
            raise ValueError("OUTPUT_SCHEMA_REF_FORMAT_MISMATCH")
        return self


class NetworkPolicySourceV1(StrEnum):
    UI_FIELD = "UI_FIELD"
    QUERY_SPAN = "QUERY_SPAN"
    SYSTEM_DEFAULT = "SYSTEM_DEFAULT"


class NetworkControlV1(StrictContractV1):
    value: Literal["DENY", "ASK", "ALLOW"]
    source: NetworkPolicySourceV1


class StructuredTaskControlsV1(StrictContractV1):
    knowledge_scope: KnowledgeScopeV1
    network_policy: NetworkControlV1
    output_contract: OutputContractV1
    selected_resource_refs: tuple[ResourceRefV1, ...] = Field(max_length=20)


class CreateTaskAttachmentV1(StrictContractV1):
    artifact_id: UUID
    sha256: Sha256Hex
    ordinal: int = Field(ge=1, le=20)


class CreateTaskRequestV1(StrictContractV1):
    conversation_id: UUID
    query: str
    attachments: tuple[CreateTaskAttachmentV1, ...] = Field(max_length=20)
    knowledge_scope: KnowledgeScopeV1 | None
    output_contract: OutputContractV1 | None
    network_policy: Literal["DENY", "ASK", "ALLOW"] | None
    selected_resource_refs: tuple[ResourceRefV1, ...] = Field(max_length=20)
    explicit_constraints: tuple[ConstraintText, ...] = Field(max_length=20)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if not 1 <= len(value) <= 32_768 or "\x00" in value:
            raise ValueError("QUERY_CHARACTER_LIMIT")
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise ValueError("QUERY_UTF8_INVALID") from error
        if len(encoded) > 65_535:
            raise ValueError("QUERY_UTF8_SIZE_LIMIT")
        return value

    @model_validator(mode="after")
    def validate_collections(self) -> CreateTaskRequestV1:
        ordinals = [item.ordinal for item in self.attachments]
        if ordinals != list(range(1, len(ordinals) + 1)):
            raise ValueError("ATTACHMENT_ORDINALS_NOT_CONTIGUOUS")
        artifact_ids = [item.artifact_id for item in self.attachments]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("ATTACHMENT_DUPLICATE_ARTIFACT")
        if len(set(self.selected_resource_refs)) != len(self.selected_resource_refs):
            raise ValueError("SELECTED_RESOURCE_DUPLICATE")
        return self


def parse_create_task_request(raw_body: bytes) -> CreateTaskRequestV1:
    if len(raw_body) > CREATE_TASK_BODY_MAX_BYTES:
        raise ValueError("CREATE_TASK_BODY_SIZE_LIMIT")
    try:
        text = raw_body.decode("utf-8", errors="strict")
        json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except UnicodeDecodeError as error:
        raise ValueError("CREATE_TASK_BODY_UTF8_INVALID") from error
    except json.JSONDecodeError as error:
        raise ValueError("CREATE_TASK_JSON_INVALID") from error
    return CreateTaskRequestV1.model_validate_json(raw_body)


def create_task_request_digest(request: CreateTaskRequestV1) -> str:
    return canonical_json_sha256(request.model_dump(mode="json"))


def normalize_knowledge_scope(value: KnowledgeScopeV1 | None) -> KnowledgeScopeV1:
    if value is not None:
        return value
    return KnowledgeScopeV1(
        mode=KnowledgeScopeModeV1.DEFAULT_AUTHORIZED,
        directory_ids=(),
        knowledge_version_ids=(),
        include_personal=False,
    )


def normalize_network_control(
    value: Literal["DENY", "ASK", "ALLOW"] | None,
) -> NetworkControlV1:
    if value is None:
        return NetworkControlV1(
            value="ASK",
            source=NetworkPolicySourceV1.SYSTEM_DEFAULT,
        )
    return NetworkControlV1(value=value, source=NetworkPolicySourceV1.UI_FIELD)


class TaskInputSnapshotV1(StrictContractV1):
    contract_version: Literal["task_input_snapshot_v1"]
    input_id: UUID
    task_id: UUID
    input_revision: int = Field(ge=1)
    message_id: UUID
    message_ref: ArtifactRefV1
    actor_id: UUID
    organization_id: UUID
    conversation_id: UUID | None
    attachment_binding_refs: tuple[TaskAttachmentBindingRefV1, ...] = Field(max_length=20)
    structured_controls: StructuredTaskControlsV1
    task_constraint_refs: tuple[ConstraintRefV1, ...] = Field(max_length=64)
    policy_snapshot_ref: PolicySnapshotRefV1
    locale: Annotated[str, StringConstraints(min_length=1, max_length=35)]
    timezone: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    snapshot_digest: Sha256Hex
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTC_DATETIME_REQUIRED")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_binding_task(self) -> TaskInputSnapshotV1:
        if any(ref.task_id != self.task_id for ref in self.attachment_binding_refs):
            raise ValueError("ATTACHMENT_BINDING_TASK_MISMATCH")
        # Constraint可显式沿用旧input_id；归属和版本由Snapshot Builder查PG核验。
        expected_digest = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"snapshot_digest"})
        )
        if self.snapshot_digest != expected_digest:
            raise ValueError("SNAPSHOT_DIGEST_MISMATCH")
        return self
