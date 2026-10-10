"""CORE-39：零LLM `PlanValidatorV1`，验证当前计划版本是否可执行并给出四类结果。

只有`ACCEPT`时`compiled_plan_digest`非空且reason/path为空；其他Outcome digest必须为空。
`REPAIRABLE_INVALID`只用于Draft可修错误，Goal事实漂移走`GOAL_RECHECK_REQUIRED`，能力/预算/
Deadline确定不可执行走`UNEXECUTABLE`。两次修复耗尽不自动放宽Schema或伪造Plan。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from .compiled_plan import compile_plan
from .planning_fragments import (
    ArtifactCriterionTargetV1,
    CapabilityPlanningViewV1,
    InitialPlanDraftV1,
    InitialPlanValidationReasonV1,
    InitialPlanValidationV1,
    validate_plan_relations,
)

PlanValidationOutcomeV1 = Literal[
    "ACCEPT", "REPAIRABLE_INVALID", "GOAL_RECHECK_REQUIRED", "UNEXECUTABLE"
]

_UNEXECUTABLE_REASONS = frozenset(
    {
        InitialPlanValidationReasonV1.CAPABILITY_REQUIREMENT_UNRESOLVED,
        InitialPlanValidationReasonV1.BUDGET_INSUFFICIENT,
        InitialPlanValidationReasonV1.DEADLINE_EXCEEDED,
    }
)
_GOAL_RECHECK_REASONS = frozenset(
    {
        InitialPlanValidationReasonV1.GOAL_BLOCKING_ISSUE,
        InitialPlanValidationReasonV1.CONSTRAINT_CONFLICT,
        InitialPlanValidationReasonV1.REFERENCE_STALE,
    }
)


def _classify(reasons: set[InitialPlanValidationReasonV1]) -> PlanValidationOutcomeV1:
    if reasons & _UNEXECUTABLE_REASONS:
        return "UNEXECUTABLE"
    if reasons & _GOAL_RECHECK_REASONS:
        return "GOAL_RECHECK_REQUIRED"
    return "REPAIRABLE_INVALID"


def _unresolved_capability_paths(
    draft: InitialPlanDraftV1, planning_view: CapabilityPlanningViewV1
) -> tuple[str, ...]:
    categories = {descriptor.category for descriptor in planning_view.descriptors}
    paths: list[str] = []
    for item_index, item in enumerate(draft.items):
        for requirement_index, requirement in enumerate(item.capability_requirements):
            if requirement.category not in categories:
                paths.append(f"/items/{item_index}/capability_requirements/{requirement_index}")
    return tuple(paths)


def validate_plan(
    *,
    task_id: UUID,
    plan_version: int,
    predecessor_version: int | None,
    input_id: UUID,
    draft: InitialPlanDraftV1,
    goal_criterion_ids: set[str],
    planning_view: CapabilityPlanningViewV1,
    deadline_at: datetime,
    now: datetime,
    budget_sufficient: bool = True,
) -> InitialPlanValidationV1:
    """验证Draft并（在ACCEPT路径）编译出`CompiledPlanV1`，返回四类结果之一。"""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("UTC_DATETIME_REQUIRED")
    if deadline_at.tzinfo is None or deadline_at.utcoffset() is None:
        raise ValueError("UTC_DATETIME_REQUIRED")
    now = now.astimezone(UTC)
    deadline_at = deadline_at.astimezone(UTC)

    issue_pairs: list[tuple[InitialPlanValidationReasonV1, str]] = [
        (issue.reason, issue.field_path)
        for issue in validate_plan_relations(draft, goal_criterion_ids)
    ]
    for path in _unresolved_capability_paths(draft, planning_view):
        issue_pairs.append((InitialPlanValidationReasonV1.CAPABILITY_REQUIREMENT_UNRESOLVED, path))
    if not budget_sufficient:
        issue_pairs.append(
            (InitialPlanValidationReasonV1.BUDGET_INSUFFICIENT, "/budget_reservation")
        )
    if now >= deadline_at:
        issue_pairs.append((InitialPlanValidationReasonV1.DEADLINE_EXCEEDED, "/deadline"))
    if not any(isinstance(c.target, ArtifactCriterionTargetV1) for c in draft.success_criteria):
        issue_pairs.append((InitialPlanValidationReasonV1.SCHEMA_INVALID, "/success_criteria"))

    if issue_pairs:
        reasons = sorted({reason for reason, _ in issue_pairs}, key=lambda reason: reason.value)
        return InitialPlanValidationV1(
            outcome=_classify(set(reasons)),
            reason_codes=tuple(reasons),
            field_paths=tuple(path for _, path in issue_pairs),
            compiled_plan_digest=None,
        )

    compiled = compile_plan(
        task_id=task_id,
        plan_version=plan_version,
        predecessor_version=predecessor_version,
        input_id=input_id,
        draft=draft,
        planning_view=planning_view,
        deadline_at=deadline_at,
    )
    return InitialPlanValidationV1(
        outcome="ACCEPT",
        reason_codes=(),
        field_paths=(),
        compiled_plan_digest=compiled.compiled_plan_digest,
    )


__all__ = [
    "PlanValidationOutcomeV1",
    "validate_plan",
]
