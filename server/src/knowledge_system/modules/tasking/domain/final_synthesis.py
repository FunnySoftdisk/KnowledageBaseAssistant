"""AGT-08 §5.28：不可变 `FinalSynthesisSnapshotV1` 契约与快照冻结。

Snapshot 由零 LLM `FinalSynthesisContextBuilderV1` 在 `START_FINAL_SYNTHESIS` 事务内构建；
不复制正文/Evidence/History/Prompt/Tool 原包，正文由 Context Assembly 按不可变目录引用物化。
本模块只做确定性冻结：RFC 8785 JCS + SHA-256 摘要、UUIDv5 快照 ID 与 65535 字节上限。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import rfc8785
from pydantic import Field, StringConstraints, field_validator

from .compiled_criterion import CompiledCriterionV1
from .core_types import (
    ArtifactRefV1,
    ConstraintRefV1,
    PolicySnapshotRefV1,
    SchemaRefV1,
    Sha256Hex,
    ShortVersion,
    StableKey,
    StrictContractV1,
    VersionedRefV1,
    canonical_json_sha256,
)
from .input_contracts import OutputContractV1

FINAL_SYNTHESIS_SNAPSHOT_NAMESPACE = uuid5(
    NAMESPACE_URL, "urn:knowledge-system:final-synthesis-snapshot:v1"
)
FINAL_SYNTHESIS_BUILDER_PROFILE = "final-synthesis-context-builder-v1"
FINAL_SYNTHESIS_SNAPSHOT_MAX_BYTES = 65_535

FinalGapKindV1 = Literal[
    "EVIDENCE_MISSING",
    "EVIDENCE_CONFLICT",
    "CAPABILITY_LIMITATION",
    "APPROVAL_OR_POLICY_LIMIT",
    "UNRESOLVED_SUBGOAL",
]


class FinalGapSummaryV1(StrictContractV1):
    gap_kind: FinalGapKindV1
    summary: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class FinalSourceVisibilityV1(StrictContractV1):
    source_ref: VersionedRefV1
    visible: bool


class FinalProtectedBudgetV1(StrictContractV1):
    model_requests: int = Field(ge=1, le=2)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class FinalSynthesisSnapshotV1(StrictContractV1):
    contract_version: Literal["final_synthesis_snapshot_v1"]
    task_id: UUID
    runtime_decision_id: UUID
    input_id: UUID
    plan_version: int = Field(ge=1)
    work_version: int = Field(ge=1)
    execution_generation: int = Field(ge=0)
    cancellation_generation: int = Field(ge=0)
    event_high_watermark: int = Field(ge=0)
    compiled_criteria: tuple[CompiledCriterionV1, ...] = Field(min_length=1, max_length=12)
    effective_constraint_refs: tuple[ConstraintRefV1, ...] = Field(max_length=32)
    output_contract: OutputContractV1
    adopted_result_refs: tuple[ArtifactRefV1, ...] = Field(max_length=50)
    gap_summaries: tuple[FinalGapSummaryV1, ...] = Field(max_length=12)
    conflict_ids: tuple[UUID, ...] = Field(max_length=16)
    unresolved_subgoal_ids: tuple[UUID, ...] = Field(max_length=16)
    permanent_blocker_ids: tuple[UUID, ...] = Field(max_length=16)
    citation_candidate_catalog_ids: tuple[StableKey, ...] = Field(max_length=200)
    result_artifact_candidate_catalog_ids: tuple[StableKey, ...] = Field(max_length=50)
    policy_snapshot_ref: PolicySnapshotRefV1
    source_visibility: tuple[FinalSourceVisibilityV1, ...] = Field(max_length=64)
    authorization_revoked: bool
    degraded: bool
    final_protected_budget: FinalProtectedBudgetV1
    deadline_at: datetime
    builder_profile: ShortVersion
    builder_schema_ref: SchemaRefV1
    snapshot_digest: Sha256Hex = "0" * 64

    @field_validator("deadline_at")
    @classmethod
    def normalize_deadline(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTC_DATETIME_REQUIRED")
        return value.astimezone(UTC)


def snapshot_digest_for(snapshot: FinalSynthesisSnapshotV1) -> str:
    return canonical_json_sha256(snapshot.model_dump(mode="json", exclude={"snapshot_digest"}))


def snapshot_canonical_bytes(snapshot: FinalSynthesisSnapshotV1) -> int:
    return len(rfc8785.dumps(snapshot.model_dump(mode="json")))


def freeze_final_synthesis_snapshot(
    snapshot: FinalSynthesisSnapshotV1,
) -> FinalSynthesisSnapshotV1:
    """以 JCS + SHA-256（排除 snapshot_digest 自身）冻结快照并强制 65535 字节上限。"""

    digest = snapshot_digest_for(snapshot)
    frozen = snapshot.model_copy(update={"snapshot_digest": digest})
    if snapshot_canonical_bytes(frozen) > FINAL_SYNTHESIS_SNAPSHOT_MAX_BYTES:
        raise ValueError("FINAL_SYNTHESIS_SNAPSHOT_TOO_LARGE")
    return frozen


def final_synthesis_snapshot_id(
    *,
    task_id: UUID,
    runtime_decision_id: UUID,
    snapshot_digest: Sha256Hex,
    builder_profile: ShortVersion,
) -> UUID:
    """同一依据（task:decision:digest:profile）必须派生同一快照 ID。"""

    name = (
        f"{str(task_id).lower()}:{str(runtime_decision_id).lower()}:"
        f"{snapshot_digest}:{builder_profile}"
    )
    return uuid5(FINAL_SYNTHESIS_SNAPSHOT_NAMESPACE, name)


__all__ = [
    "FINAL_SYNTHESIS_BUILDER_PROFILE",
    "FINAL_SYNTHESIS_SNAPSHOT_MAX_BYTES",
    "FINAL_SYNTHESIS_SNAPSHOT_NAMESPACE",
    "FinalGapKindV1",
    "FinalGapSummaryV1",
    "FinalProtectedBudgetV1",
    "FinalSourceVisibilityV1",
    "FinalSynthesisSnapshotV1",
    "final_synthesis_snapshot_id",
    "freeze_final_synthesis_snapshot",
    "snapshot_canonical_bytes",
    "snapshot_digest_for",
]
