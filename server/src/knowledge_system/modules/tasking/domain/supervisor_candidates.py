"""DEV-05：Supervisor模型候选与最终定义分离。

模型只生成「候选」内容子集（`GoalUnderstandingModelOutputV1`/`InitialPlanModelOutputV1`），
服务端再注入Context中的权威字段（`input_id`/`effective_constraint_refs`/
`goal_understanding_ref`）装配为完整契约。候选不含任何服务端才能确定的身份/引用/Profile，
模型不得声明Task/Input/Goal Understanding Artifact的身份，也不执行I/O或授权。

`goal_understanding_id`按内容寻址`UUIDv5`派生：同一理解内容同一ID，重放稳定。
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, StringConstraints, model_validator

from .core_types import ResourceRefV1, Sha256Hex, StrictContractV1
from .goal_contracts import (
    GoalBlockingIssueV1,
    GoalCriterionProposalV1,
    GoalUnderstandingContextV1,
    GoalUnderstandingV1,
    LocalId,
    SafeAssumptionV1,
    SubgoalV1,
)
from .planning_fragments import (
    CriterionDraftV1,
    HypothesisDraftV1,
    InitialPlanDraftV1,
    InitialPlanningContextV1,
    PlanItemDraftV1,
)

GOAL_UNDERSTANDING_NAMESPACE = uuid5(
    NAMESPACE_URL, "urn:knowledge-system:goal-understanding:v1"
)


class GoalUnderstandingModelOutputV1(StrictContractV1):
    """模型生成的Goal理解内容子集；服务端字段（input_id/约束引用）由装配注入。"""

    contract_version: Literal["supervisor_goal_understanding_model_output_v1"]
    primary_goal: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    expected_deliverable: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    subgoals: tuple[SubgoalV1, ...] = Field(max_length=8)
    resource_refs: tuple[ResourceRefV1, ...] = Field(max_length=20)
    success_criteria_proposals: tuple[GoalCriterionProposalV1, ...] = Field(
        min_length=1, max_length=12
    )
    safe_assumptions: tuple[SafeAssumptionV1, ...] = Field(max_length=8)
    blocking_issues: tuple[GoalBlockingIssueV1, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_local_ids(self) -> GoalUnderstandingModelOutputV1:
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


class InitialPlanModelOutputV1(StrictContractV1):
    """模型生成的首轮Plan内容子集；`goal_understanding_ref`/`prompt_profile`由装配注入。"""

    contract_version: Literal["supervisor_initial_plan_model_output_v1"]
    hypotheses: tuple[HypothesisDraftV1, ...] = Field(max_length=8)
    items: tuple[PlanItemDraftV1, ...] = Field(min_length=1, max_length=12)
    success_criteria: tuple[CriterionDraftV1, ...] = Field(min_length=1, max_length=12)
    initial_ready_item_local_ids: tuple[LocalId, ...] = Field(min_length=1, max_length=3)


def assemble_goal_understanding(
    output: GoalUnderstandingModelOutputV1, context: GoalUnderstandingContextV1
) -> GoalUnderstandingV1:
    """把候选装配为完整Goal理解，注入Context中的权威身份与约束引用。"""

    return GoalUnderstandingV1(
        contract_version="supervisor_goal_understanding_v1",
        input_id=context.input_id,
        primary_goal=output.primary_goal,
        expected_deliverable=output.expected_deliverable,
        subgoals=output.subgoals,
        effective_constraint_refs=context.effective_constraint_refs,
        resource_refs=output.resource_refs,
        success_criteria_proposals=output.success_criteria_proposals,
        safe_assumptions=output.safe_assumptions,
        blocking_issues=output.blocking_issues,
    )


def assemble_initial_plan(
    output: InitialPlanModelOutputV1, context: InitialPlanningContextV1
) -> InitialPlanDraftV1:
    """把候选装配为首轮Plan Draft，注入Goal理解引用与Profile。"""

    return InitialPlanDraftV1(
        contract_version="supervisor_initial_plan_v1",
        goal_understanding_ref=context.goal_understanding_ref,
        hypotheses=output.hypotheses,
        items=output.items,
        success_criteria=output.success_criteria,
        initial_ready_item_local_ids=output.initial_ready_item_local_ids,
        prompt_profile="supervisor-initial-plan-v1",
    )


def goal_understanding_id(understanding_digest: Sha256Hex) -> UUID:
    """Goal理解内容寻址ID：同内容同ID，重放稳定。"""

    return uuid5(GOAL_UNDERSTANDING_NAMESPACE, understanding_digest)


__all__ = [
    "GOAL_UNDERSTANDING_NAMESPACE",
    "GoalUnderstandingModelOutputV1",
    "InitialPlanModelOutputV1",
    "assemble_goal_understanding",
    "assemble_initial_plan",
    "goal_understanding_id",
]
