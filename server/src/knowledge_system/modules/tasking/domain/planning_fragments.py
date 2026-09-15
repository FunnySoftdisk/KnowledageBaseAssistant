"""CORE-34～40：首轮Plan严格契约及不依赖I/O的关系校验。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, StringConstraints, TypeAdapter, field_validator, model_validator

from .core_types import (
    ArtifactRefV1,
    CapabilityRefV1,
    ConstraintRefV1,
    PolicySnapshotRefV1,
    ResourceRefV1,
    Sha256Hex,
    ShortVersion,
    StableKey,
    StrictContractV1,
)
from .goal_contracts import LocalId

PLAN_ITEM_NAMESPACE = uuid5(NAMESPACE_URL, "urn:knowledge-system:task-plan-local:v1")
LOCAL_ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}")


class CapabilityRequirementV1(StrictContractV1):
    category: StableKey
    required_input_kinds: tuple[StableKey, ...]
    required_output_kind: StableKey


class IterationPolicyV1(StrictContractV1):
    mode: Literal["SINGLE_PASS", "BOUNDED_REPEAT"]
    max_iterations: int = Field(ge=1, le=3)
    stop_criterion_ids: tuple[LocalId, ...] = Field(max_length=12)

    @model_validator(mode="after")
    def validate_branch(self) -> IterationPolicyV1:
        if self.mode == "SINGLE_PASS" and (self.max_iterations != 1 or self.stop_criterion_ids):
            raise ValueError("SINGLE_PASS_RELATION_INVALID")
        if self.mode == "BOUNDED_REPEAT" and (
            self.max_iterations < 2 or not self.stop_criterion_ids
        ):
            raise ValueError("BOUNDED_REPEAT_RELATION_INVALID")
        if len(set(self.stop_criterion_ids)) != len(self.stop_criterion_ids):
            raise ValueError("DUPLICATE_STOP_CRITERION")
        return self


class HypothesisDraftV1(StrictContractV1):
    local_id: LocalId
    statement: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class ArtifactCriterionTargetV1(StrictContractV1):
    kind: Literal["ARTIFACT"]
    producer_item_local_id: LocalId
    output_schema_key: StableKey


class CitationCriterionTargetV1(StrictContractV1):
    kind: Literal["CITATION"]
    producer_item_local_id: LocalId
    min_citations: int = Field(ge=1, le=16)


class StateCriterionTargetV1(StrictContractV1):
    kind: Literal["STATE"]
    subject_item_local_id: LocalId
    expected_state: Literal["COMPLETED"]


class UserCriterionTargetV1(StrictContractV1):
    kind: Literal["USER"]
    constraint_ref: ConstraintRefV1


CriterionTargetV1 = Annotated[
    ArtifactCriterionTargetV1
    | CitationCriterionTargetV1
    | StateCriterionTargetV1
    | UserCriterionTargetV1,
    Field(discriminator="kind"),
]
CRITERION_TARGET_ADAPTER: TypeAdapter[CriterionTargetV1] = TypeAdapter(CriterionTargetV1)


class CriterionDraftV1(StrictContractV1):
    local_id: LocalId
    statement: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    goal_criterion_local_id: LocalId | None
    target: CriterionTargetV1


class PlanItemDraftV1(StrictContractV1):
    local_id: LocalId
    objective: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    executor_requirement: Literal["SUPERVISOR", "SPECIALIST", "DYNAMIC_ALLOWED"]
    capability_requirements: tuple[CapabilityRequirementV1, ...]
    input_artifact_refs: tuple[ArtifactRefV1, ...] = Field(max_length=20)
    output_schema_ref: StableKey
    depends_on_local_ids: tuple[LocalId, ...] = Field(max_length=12)
    iteration_policy: IterationPolicyV1


class InitialPlanDraftV1(StrictContractV1):
    contract_version: Literal["supervisor_initial_plan_v1"]
    goal_understanding_ref: ArtifactRefV1
    hypotheses: tuple[HypothesisDraftV1, ...] = Field(max_length=8)
    items: tuple[PlanItemDraftV1, ...] = Field(min_length=1, max_length=12)
    success_criteria: tuple[CriterionDraftV1, ...] = Field(min_length=1, max_length=12)
    initial_ready_item_local_ids: tuple[LocalId, ...] = Field(min_length=1, max_length=3)
    prompt_profile: Literal["supervisor-initial-plan-v1"]


class CapabilityPlanningDescriptorV1(StrictContractV1):
    capability_ref: CapabilityRefV1
    category: StableKey
    safe_summary: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    input_schema_key: StableKey
    output_schema_key: StableKey
    allowed_executor_kinds: tuple[Literal["SUPERVISOR", "BUILT_IN", "EPHEMERAL"], ...]


class CapabilityPlanningViewV1(StrictContractV1):
    contract_version: Literal["capability_planning_view_v1"]
    catalog_version: ShortVersion
    catalog_digest: Sha256Hex
    descriptors: tuple[CapabilityPlanningDescriptorV1, ...] = Field(max_length=32)


class InitialPlanningContextV1(StrictContractV1):
    contract_version: Literal["supervisor_initial_plan_v1"]
    task_id: UUID
    input_id: UUID
    goal_understanding_ref: ArtifactRefV1
    effective_constraint_refs: tuple[ConstraintRefV1, ...] = Field(max_length=64)
    output_contract_ref: ArtifactRefV1
    resource_refs: tuple[ResourceRefV1, ...] = Field(max_length=20)
    context_pack_ref: ArtifactRefV1
    capability_planning_view_ref: ArtifactRefV1
    budget_reservation_ref: ArtifactRefV1
    policy_snapshot_ref: PolicySnapshotRefV1
    deadline_at: datetime
    prompt_profile: Literal["supervisor-initial-plan-v1"]

    @field_validator("deadline_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTC_DATETIME_REQUIRED")
        return value.astimezone(UTC)


class InitialPlanValidationReasonV1(StrEnum):
    SCHEMA_INVALID = "SCHEMA_INVALID"
    REFERENCE_STALE = "REFERENCE_STALE"
    DUPLICATE_LOCAL_ID = "DUPLICATE_LOCAL_ID"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    INITIAL_READY_INVALID = "INITIAL_READY_INVALID"
    CRITERION_COVERAGE_MISSING = "CRITERION_COVERAGE_MISSING"
    CAPABILITY_REQUIREMENT_UNRESOLVED = "CAPABILITY_REQUIREMENT_UNRESOLVED"
    CONSTRAINT_CONFLICT = "CONSTRAINT_CONFLICT"
    BUDGET_INSUFFICIENT = "BUDGET_INSUFFICIENT"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    GOAL_BLOCKING_ISSUE = "GOAL_BLOCKING_ISSUE"
    FORBIDDEN_AUTHORITY_FIELD = "FORBIDDEN_AUTHORITY_FIELD"
    SIZE_LIMIT_EXCEEDED = "SIZE_LIMIT_EXCEEDED"


class InitialPlanValidationV1(StrictContractV1):
    outcome: Literal["ACCEPT", "REPAIRABLE_INVALID", "GOAL_RECHECK_REQUIRED", "UNEXECUTABLE"]
    reason_codes: tuple[InitialPlanValidationReasonV1, ...]
    field_paths: tuple[str, ...]
    compiled_plan_digest: Sha256Hex | None

    @model_validator(mode="after")
    def validate_outcome(self) -> InitialPlanValidationV1:
        if self.outcome == "ACCEPT":
            if self.reason_codes or self.field_paths or self.compiled_plan_digest is None:
                raise ValueError("ACCEPT_RELATION_INVALID")
        elif self.compiled_plan_digest is not None or not self.reason_codes:
            raise ValueError("REJECTION_RELATION_INVALID")
        return self


class PlanSemanticIssueV1(StrictContractV1):
    reason: InitialPlanValidationReasonV1
    field_path: str


def plan_item_id(task_id: UUID, plan_version: int, source_local_id: str) -> UUID:
    if plan_version < 1 or LOCAL_ID_PATTERN.fullmatch(source_local_id) is None:
        raise ValueError("PLAN_ITEM_ID_INPUT_INVALID")
    return uuid5(PLAN_ITEM_NAMESPACE, f"{str(task_id).lower()}:{plan_version}:{source_local_id}")


def validate_plan_relations(
    draft: InitialPlanDraftV1, goal_criterion_ids: set[str]
) -> tuple[PlanSemanticIssueV1, ...]:
    issues: list[PlanSemanticIssueV1] = []
    item_ids = [item.local_id for item in draft.items]
    criterion_ids = [criterion.local_id for criterion in draft.success_criteria]
    hypothesis_ids = [hypothesis.local_id for hypothesis in draft.hypotheses]
    for field_name, values in (
        ("items", item_ids),
        ("success_criteria", criterion_ids),
        ("hypotheses", hypothesis_ids),
        ("initial_ready_item_local_ids", list(draft.initial_ready_item_local_ids)),
    ):
        if len(values) != len(set(values)):
            issues.append(
                PlanSemanticIssueV1(
                    reason=InitialPlanValidationReasonV1.DUPLICATE_LOCAL_ID,
                    field_path=f"/{field_name}",
                )
            )
    known_items = set(item_ids)
    known_criteria = set(criterion_ids)
    graph: dict[str, tuple[str, ...]] = {}
    for index, item in enumerate(draft.items):
        graph[item.local_id] = item.depends_on_local_ids
        if (
            item.local_id in item.depends_on_local_ids
            or not set(item.depends_on_local_ids) <= known_items
        ):
            issues.append(
                PlanSemanticIssueV1(
                    reason=InitialPlanValidationReasonV1.DEPENDENCY_MISSING,
                    field_path=f"/items/{index}/depends_on_local_ids",
                )
            )
        if not set(item.iteration_policy.stop_criterion_ids) <= known_criteria:
            issues.append(
                PlanSemanticIssueV1(
                    reason=InitialPlanValidationReasonV1.DEPENDENCY_MISSING,
                    field_path=f"/items/{index}/iteration_policy/stop_criterion_ids",
                )
            )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited or node not in graph:
            return False
        visiting.add(node)
        cycle = any(visit(parent) for parent in graph[node])
        visiting.remove(node)
        visited.add(node)
        return cycle

    if any(visit(node) for node in graph):
        issues.append(
            PlanSemanticIssueV1(
                reason=InitialPlanValidationReasonV1.DEPENDENCY_CYCLE,
                field_path="/items",
            )
        )
    ready = set(draft.initial_ready_item_local_ids)
    if not ready <= known_items or any(graph.get(item_id) for item_id in ready):
        issues.append(
            PlanSemanticIssueV1(
                reason=InitialPlanValidationReasonV1.INITIAL_READY_INVALID,
                field_path="/initial_ready_item_local_ids",
            )
        )
    covered_goal_ids: list[str] = []
    for index, criterion in enumerate(draft.success_criteria):
        target = criterion.target
        producer = getattr(target, "producer_item_local_id", None) or getattr(
            target, "subject_item_local_id", None
        )
        if producer is not None and producer not in known_items:
            issues.append(
                PlanSemanticIssueV1(
                    reason=InitialPlanValidationReasonV1.DEPENDENCY_MISSING,
                    field_path=f"/success_criteria/{index}/target",
                )
            )
        if criterion.goal_criterion_local_id is not None:
            covered_goal_ids.append(criterion.goal_criterion_local_id)
    if set(covered_goal_ids) != goal_criterion_ids or len(covered_goal_ids) != len(
        set(covered_goal_ids)
    ):
        issues.append(
            PlanSemanticIssueV1(
                reason=InitialPlanValidationReasonV1.CRITERION_COVERAGE_MISSING,
                field_path="/success_criteria",
            )
        )
    return tuple(issues)
