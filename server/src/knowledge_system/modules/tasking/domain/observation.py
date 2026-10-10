"""AGT-06 Observation 不可变投影与 Disposition 契约（零LLM、零I/O）。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, StringConstraints, TypeAdapter, field_validator, model_validator

from .core_types import (
    CapabilityRefV1,
    Sha256Hex,
    StableKey,
    StrictContractV1,
    VersionedRefV1,
)

TASK_OBSERVATION_NAMESPACE = uuid5(NAMESPACE_URL, "urn:knowledge-system:task-observation:v1")

# 冻结的投影画像版本；新来源必须先升级投影契约，不能以自由字符串旁路。
PROJECTION_PROFILE_VERSION = "task-observation-projection-v1"


class ObservationSourceKindV1(StrEnum):
    """Observation 的十一条正式来源；Factory 据 source_kind 选择 payload 分支。"""

    RESULT_ADOPTED = "RESULT_ADOPTED"
    RESULT_NOT_ADOPTED = "RESULT_NOT_ADOPTED"
    ACTION_TERMINAL = "ACTION_TERMINAL"
    APPROVAL_TERMINAL = "APPROVAL_TERMINAL"
    EXTERNAL_EFFECT_CHANGED = "EXTERNAL_EFFECT_CHANGED"
    TASK_INPUT_ADVANCED = "TASK_INPUT_ADVANCED"
    RESOURCE_AUTHORIZATION_CHANGED = "RESOURCE_AUTHORIZATION_CHANGED"
    CAPABILITY_AVAILABILITY_CHANGED = "CAPABILITY_AVAILABILITY_CHANGED"
    BUDGET_THRESHOLD_REACHED = "BUDGET_THRESHOLD_REACHED"
    TASK_DEADLINE_REACHED = "TASK_DEADLINE_REACHED"
    AGENT_ASSESSMENT_REPORTED = "AGENT_ASSESSMENT_REPORTED"


class ObservationClassV1(StrEnum):
    """Observation 的四条信任通道；由 source_kind 固定推导，不可被调用者另传。"""

    ADOPTED_RESULT = "ADOPTED_RESULT"
    SYSTEM_FACT = "SYSTEM_FACT"
    USER_INPUT = "USER_INPUT"
    AGENT_ASSESSMENT = "AGENT_ASSESSMENT"


class ObservationDispositionV1(StrEnum):
    RESOLVED = "RESOLVED"
    SUPERSEDED = "SUPERSEDED"
    INVALIDATED = "INVALIDATED"


_SOURCE_TO_CLASS: dict[ObservationSourceKindV1, ObservationClassV1] = {
    ObservationSourceKindV1.RESULT_ADOPTED: ObservationClassV1.ADOPTED_RESULT,
    ObservationSourceKindV1.TASK_INPUT_ADVANCED: ObservationClassV1.USER_INPUT,
    ObservationSourceKindV1.AGENT_ASSESSMENT_REPORTED: ObservationClassV1.AGENT_ASSESSMENT,
}


def observation_class_for(source_kind: ObservationSourceKindV1) -> ObservationClassV1:
    """来源与信任通道的映射不可配置；其余八类全部为 SYSTEM_FACT。"""

    return _SOURCE_TO_CLASS.get(source_kind, ObservationClassV1.SYSTEM_FACT)


class AgentReportItemV1(StrictContractV1):
    kind: Literal["EVIDENCE_GAP", "CONFLICT", "LIMITATION", "UNRESOLVED_SUBGOAL"]
    statement: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    refs: tuple[VersionedRefV1, ...] = Field(max_length=16)


class AttemptAssessmentV1(StrictContractV1):
    """Agent 在结构化终态中附带的建议性评估；不得成为完成事实或授权依据。"""

    contract_version: Literal["attempt_assessment_v1"]
    task_attempt_id: UUID
    plan_version: int = Field(ge=1)
    plan_item_id: UUID
    action_run_group_id: UUID
    progress: Literal["ACHIEVED", "PARTIAL", "NO_PROGRESS", "BLOCKED"]
    report_items: tuple[AgentReportItemV1, ...] = Field(max_length=16)
    claimed_satisfied_criterion_refs: tuple[StableKey, ...] = Field(max_length=12)
    supporting_result_refs: tuple[VersionedRefV1, ...] = Field(max_length=16)
    assessment_digest: Sha256Hex


class ResultAdoptedPayloadV1(StrictContractV1):
    source_kind: Literal["RESULT_ADOPTED"]
    adopted_refs: tuple[VersionedRefV1, ...] = Field(min_length=1, max_length=16)


class ResultNotAdoptedPayloadV1(StrictContractV1):
    source_kind: Literal["RESULT_NOT_ADOPTED"]
    outcome: StableKey
    reason_code: StableKey


class ActionTerminalPayloadV1(StrictContractV1):
    source_kind: Literal["ACTION_TERMINAL"]
    action_attempt_id: UUID
    terminal_state: StableKey
    reason_code: StableKey | None = None


class ApprovalTerminalPayloadV1(StrictContractV1):
    source_kind: Literal["APPROVAL_TERMINAL"]
    approval_ref: VersionedRefV1
    decision: StableKey
    reason_code: StableKey | None = None


class ExternalEffectChangedPayloadV1(StrictContractV1):
    source_kind: Literal["EXTERNAL_EFFECT_CHANGED"]
    invocation_id: UUID
    effect_state: StableKey
    reason_code: StableKey | None = None


class TaskInputAdvancedPayloadV1(StrictContractV1):
    source_kind: Literal["TASK_INPUT_ADVANCED"]
    input_id: UUID
    input_version: int = Field(ge=1)
    supersedes_input_id: UUID | None = None


class ResourceAuthorizationChangedPayloadV1(StrictContractV1):
    source_kind: Literal["RESOURCE_AUTHORIZATION_CHANGED"]
    resource_ref: VersionedRefV1
    authorization_state: StableKey
    reason_code: StableKey | None = None


class CapabilityAvailabilityChangedPayloadV1(StrictContractV1):
    source_kind: Literal["CAPABILITY_AVAILABILITY_CHANGED"]
    capability_ref: CapabilityRefV1
    availability_state: StableKey
    reason_code: StableKey | None = None


class BudgetThresholdReachedPayloadV1(StrictContractV1):
    source_kind: Literal["BUDGET_THRESHOLD_REACHED"]
    budget_kind: StableKey
    threshold_state: StableKey


class TaskDeadlineReachedPayloadV1(StrictContractV1):
    source_kind: Literal["TASK_DEADLINE_REACHED"]
    deadline_at: datetime
    reason_code: StableKey

    @field_validator("deadline_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTC_DATETIME_REQUIRED")
        return value.astimezone(UTC)


class AgentAssessmentReportedPayloadV1(StrictContractV1):
    source_kind: Literal["AGENT_ASSESSMENT_REPORTED"]
    assessment: AttemptAssessmentV1


ObservationPayloadV1 = Annotated[
    ResultAdoptedPayloadV1
    | ResultNotAdoptedPayloadV1
    | ActionTerminalPayloadV1
    | ApprovalTerminalPayloadV1
    | ExternalEffectChangedPayloadV1
    | TaskInputAdvancedPayloadV1
    | ResourceAuthorizationChangedPayloadV1
    | CapabilityAvailabilityChangedPayloadV1
    | BudgetThresholdReachedPayloadV1
    | TaskDeadlineReachedPayloadV1
    | AgentAssessmentReportedPayloadV1,
    Field(discriminator="source_kind"),
]
OBSERVATION_PAYLOAD_ADAPTER: TypeAdapter[ObservationPayloadV1] = TypeAdapter(
    ObservationPayloadV1
)


class TaskObservationV1(StrictContractV1):
    contract_version: Literal["task_observation_v1"]
    observation_id: UUID
    task_id: UUID
    source_event_id: UUID
    source_event_sequence: int = Field(ge=1)
    source_ordinal: int = Field(ge=0)
    source_kind: ObservationSourceKindV1
    observation_class: ObservationClassV1
    source_plan_version: Annotated[int, Field(ge=1)] | None = None
    created_at: datetime
    payload: ObservationPayloadV1
    integrity_digest: Sha256Hex

    @field_validator("created_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTC_DATETIME_REQUIRED")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_derived_class(self) -> TaskObservationV1:
        if self.observation_class != observation_class_for(self.source_kind):
            raise ValueError("OBSERVATION_CLASS_SOURCE_MISMATCH")
        if str(self.source_kind.value) != self.payload.source_kind:
            raise ValueError("OBSERVATION_PAYLOAD_SOURCE_MISMATCH")
        return self


class ObservationDispositionEventV1(StrictContractV1):
    contract_version: Literal["observation_disposition_event_v1"]
    disposition_event_id: UUID
    observation_id: UUID
    task_id: UUID
    disposition: ObservationDispositionV1
    reason_code: StableKey
    caused_by_event_id: UUID
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTC_DATETIME_REQUIRED")
        return value.astimezone(UTC)


def observation_id(
    task_id: UUID,
    source_event_id: UUID,
    source_ordinal: int,
    projection_profile_version: str,
) -> UUID:
    """Observation 稳定 ID；排序与去重均不依赖时间戳或语义合并。"""

    if source_ordinal < 0:
        raise ValueError("OBSERVATION_ID_INPUT_INVALID")
    name = (
        f"{str(task_id).lower()}:{str(source_event_id).lower()}:"
        f"{source_ordinal}:{projection_profile_version}"
    )
    return uuid5(TASK_OBSERVATION_NAMESPACE, name)


def observation_sort_key(
    observation: TaskObservationV1,
) -> tuple[int, int, UUID]:
    """排序只使用 source_event_sequence、source_ordinal、observation_id。"""

    return (
        observation.source_event_sequence,
        observation.source_ordinal,
        observation.observation_id,
    )
