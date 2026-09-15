"""CORE-26～33：Supervisor目标理解及确定性Gate输出。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from .core_types import (
    ArtifactRefV1,
    ConstraintRefV1,
    PolicySnapshotRefV1,
    ResourceRefV1,
    Sha256Hex,
    StrictContractV1,
)
from .input_contracts import StructuredTaskControlsV1

LocalId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


class GoalCriterionProposalV1(StrictContractV1):
    local_id: LocalId
    statement: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    evidence_kind: Literal["ARTIFACT", "CITATION", "STATE", "USER"]


class SafeAssumptionReasonV1(StrEnum):
    REVERSIBLE_DEFAULT = "REVERSIBLE_DEFAULT"
    NO_SCOPE_EXPANSION = "NO_SCOPE_EXPANSION"
    NO_MATERIAL_DELIVERABLE_CHANGE = "NO_MATERIAL_DELIVERABLE_CHANGE"


class SafeAssumptionV1(StrictContractV1):
    local_id: LocalId
    statement: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    reason: SafeAssumptionReasonV1


class GoalBlockingReasonV1(StrEnum):
    GOAL_AMBIGUOUS = "GOAL_AMBIGUOUS"
    RESOURCE_SELECTION_REQUIRED = "RESOURCE_SELECTION_REQUIRED"
    REQUIRED_PARAMETER_MISSING = "REQUIRED_PARAMETER_MISSING"
    CONSTRAINT_CONFLICT = "CONSTRAINT_CONFLICT"
    OUTPUT_CHOICE_MATERIAL = "OUTPUT_CHOICE_MATERIAL"


class GoalBlockingIssueV1(StrictContractV1):
    local_id: LocalId
    reason: GoalBlockingReasonV1
    field_path: Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=r"^/")]
    description: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    proposed_question_kind: Literal[
        "SINGLE_CHOICE", "MULTI_CHOICE", "SHORT_TEXT", "RESOURCE_SELECTION"
    ]
    candidate_resource_refs: tuple[ResourceRefV1, ...] = Field(max_length=20)


class SubgoalV1(StrictContractV1):
    local_id: LocalId
    statement: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class GoalUnderstandingContextV1(StrictContractV1):
    contract_version: Literal["goal_understanding_context_v1"]
    task_id: UUID
    input_id: UUID
    input_snapshot_digest: Sha256Hex
    query_ref: ArtifactRefV1
    structured_controls: StructuredTaskControlsV1
    effective_constraint_refs: tuple[ConstraintRefV1, ...] = Field(max_length=64)
    authorized_resource_refs: tuple[ResourceRefV1, ...] = Field(max_length=20)
    classifier_annotation_ref: ArtifactRefV1 | None
    policy_snapshot_ref: PolicySnapshotRefV1
    deadline_at: datetime
    prompt_profile: Literal["supervisor-goal-understanding-v1"]
    schema_version: Literal["supervisor_goal_understanding_v1"]

    @field_validator("deadline_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTC_DATETIME_REQUIRED")
        return value.astimezone(UTC)


class GoalUnderstandingV1(StrictContractV1):
    contract_version: Literal["supervisor_goal_understanding_v1"]
    input_id: UUID
    primary_goal: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    expected_deliverable: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    subgoals: tuple[SubgoalV1, ...] = Field(max_length=8)
    effective_constraint_refs: tuple[ConstraintRefV1, ...] = Field(max_length=64)
    resource_refs: tuple[ResourceRefV1, ...] = Field(max_length=20)
    success_criteria_proposals: tuple[GoalCriterionProposalV1, ...] = Field(
        min_length=1, max_length=12
    )
    safe_assumptions: tuple[SafeAssumptionV1, ...] = Field(max_length=8)
    blocking_issues: tuple[GoalBlockingIssueV1, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_local_ids(self) -> GoalUnderstandingV1:
        for values in (
            self.subgoals,
            self.success_criteria_proposals,
            self.safe_assumptions,
            self.blocking_issues,
        ):
            ids = [value.local_id for value in values]
            if len(ids) != len(set(ids)):
                raise ValueError("DUPLICATE_LOCAL_ID")
        return self


class GoalGateDecisionV1(StrictContractV1):
    understanding_id: UUID
    understanding_digest: Sha256Hex
    outcome: Literal["READY_TO_PLAN", "REQUEST_USER_INPUT"]
    validated_issue_ids: tuple[LocalId, ...] = Field(max_length=3)
    accepted_assumption_ids: tuple[LocalId, ...] = Field(max_length=8)
    reason_codes: tuple[GoalBlockingReasonV1, ...] = Field(max_length=3)
    gate_profile_digest: Sha256Hex

    @model_validator(mode="after")
    def validate_outcome(self) -> GoalGateDecisionV1:
        if self.outcome == "REQUEST_USER_INPUT" and not self.validated_issue_ids:
            raise ValueError("REQUEST_REQUIRES_VALIDATED_ISSUE")
        if self.outcome == "READY_TO_PLAN" and (self.validated_issue_ids or self.reason_codes):
            raise ValueError("READY_CANNOT_HAVE_BLOCKING_ISSUE")
        return self


class GoalSemanticReasonV1(StrEnum):
    INPUT_MISMATCH = "INPUT_MISMATCH"
    EFFECTIVE_CONSTRAINT_SET_MISMATCH = "EFFECTIVE_CONSTRAINT_SET_MISMATCH"
    UNKNOWN_RESOURCE_REF = "UNKNOWN_RESOURCE_REF"
    DUPLICATE_REF = "DUPLICATE_REF"


class GoalSemanticIssueV1(StrictContractV1):
    reason: GoalSemanticReasonV1
    field_path: str


def validate_goal_relations(
    goal: GoalUnderstandingV1, context: GoalUnderstandingContextV1
) -> tuple[GoalSemanticIssueV1, ...]:
    """验证模型只能复制当前Context中的事实引用，不执行I/O或授权。"""

    issues: list[GoalSemanticIssueV1] = []
    if goal.input_id != context.input_id:
        issues.append(
            GoalSemanticIssueV1(
                reason=GoalSemanticReasonV1.INPUT_MISMATCH,
                field_path="/input_id",
            )
        )
    if set(goal.effective_constraint_refs) != set(context.effective_constraint_refs):
        issues.append(
            GoalSemanticIssueV1(
                reason=GoalSemanticReasonV1.EFFECTIVE_CONSTRAINT_SET_MISMATCH,
                field_path="/effective_constraint_refs",
            )
        )
    if len(set(goal.resource_refs)) != len(goal.resource_refs):
        issues.append(
            GoalSemanticIssueV1(
                reason=GoalSemanticReasonV1.DUPLICATE_REF,
                field_path="/resource_refs",
            )
        )
    known_resources = set(context.authorized_resource_refs)
    for index, resource_ref in enumerate(goal.resource_refs):
        if resource_ref not in known_resources:
            issues.append(
                GoalSemanticIssueV1(
                    reason=GoalSemanticReasonV1.UNKNOWN_RESOURCE_REF,
                    field_path=f"/resource_refs/{index}",
                )
            )
    for issue_index, blocking_issue in enumerate(goal.blocking_issues):
        for resource_index, resource_ref in enumerate(blocking_issue.candidate_resource_refs):
            if resource_ref not in known_resources:
                issues.append(
                    GoalSemanticIssueV1(
                        reason=GoalSemanticReasonV1.UNKNOWN_RESOURCE_REF,
                        field_path=(
                            f"/blocking_issues/{issue_index}/candidate_resource_refs/"
                            f"{resource_index}"
                        ),
                    )
                )
    return tuple(issues)
