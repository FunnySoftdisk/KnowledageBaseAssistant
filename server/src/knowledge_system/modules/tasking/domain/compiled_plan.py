"""CORE-40：零LLM `PlanCompilerV1`，把`InitialPlanDraftV1`编译为不可变`CompiledPlanV1`。

`PlanCompilerV1`负责分配Plan Item领域ID、把局部依赖转换为UUID、从规划视图解析候选Capability、
把成功条件编译为Criterion，并注入Budget/Deadline/User Abort/No Progress/Conflict等确定性Stop
Condition。Plan Item ID使用项目固定UUIDv5 Namespace和`task_id:plan_version:source_local_id`生成，
保证同一输入重放得到相同ID及Compiled Digest。模型不能选择具体Agent实例、Capability、Grant、
Approval或R0–R3风险。

候选Capability按`category`匹配解析（编译期候选，不是授权）；输入/输出Schema兼容在ReadySet/
ExecutorResolver阶段用完整目录复核。首轮计划只允许`version=1/predecessor=None/reason=NEW_TASK`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, StringConstraints, model_validator

from .compiled_criterion import CompiledCriterionV1, compile_criterion, derive_criterion_source
from .core_types import (
    ArtifactRefV1,
    CapabilityRefV1,
    Sha256Hex,
    StableKey,
    StrictContractV1,
    canonical_json_sha256,
)
from .goal_contracts import LocalId
from .planning_fragments import (
    LOCAL_ID_PATTERN,
    CapabilityPlanningDescriptorV1,
    CapabilityPlanningViewV1,
    CapabilityRequirementV1,
    InitialPlanDraftV1,
    IterationPolicyV1,
    plan_item_id,
)

HYPOTHESIS_NAMESPACE = uuid5(NAMESPACE_URL, "urn:knowledge-system:task-plan-hypothesis:v1")
MAX_NO_PROGRESS_ROUNDS = 2


class CompiledHypothesisV1(StrictContractV1):
    hypothesis_id: UUID
    source_local_id: LocalId
    statement: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    initial_status: Literal["OPEN"]
    hypothesis_digest: Sha256Hex


class CompiledPlanItemV1(StrictContractV1):
    plan_item_id: UUID
    source_local_id: LocalId
    objective: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    executor_requirement: Literal["SUPERVISOR", "SPECIALIST", "DYNAMIC_ALLOWED"]
    capability_requirements: tuple[CapabilityRequirementV1, ...]
    candidate_capability_refs: tuple[CapabilityRefV1, ...]
    input_artifact_refs: tuple[ArtifactRefV1, ...] = Field(max_length=20)
    output_schema_ref: StableKey
    depends_on_item_ids: tuple[UUID, ...] = Field(max_length=12)
    iteration_policy: IterationPolicyV1
    initial_state: Literal["OPEN", "READY"]
    definition_digest: Sha256Hex


class StopConditionKindV1(StrEnum):
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    NO_PROGRESS = "NO_PROGRESS"
    USER_ABORT = "USER_ABORT"
    DEADLINE = "DEADLINE"
    CONFLICT_UNRESOLVABLE = "CONFLICT_UNRESOLVABLE"


class StopConditionV1(StrictContractV1):
    stop_condition_id: LocalId
    kind: StopConditionKindV1
    detail: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class CompiledPlanV1(StrictContractV1):
    contract_version: Literal["task_plan_version_v1"]
    task_id: UUID
    plan_version: int = Field(ge=1)
    predecessor_version: int | None
    input_id: UUID
    goal_understanding_ref: ArtifactRefV1
    hypotheses: tuple[CompiledHypothesisV1, ...] = Field(max_length=8)
    items: tuple[CompiledPlanItemV1, ...] = Field(min_length=1, max_length=12)
    compiled_criteria: tuple[CompiledCriterionV1, ...] = Field(min_length=1, max_length=12)
    stop_conditions: tuple[StopConditionV1, ...]
    initial_ready_item_ids: tuple[UUID, ...] = Field(min_length=1, max_length=3)
    revision_reason: Literal["NEW_TASK"]
    compiled_plan_digest: Sha256Hex

    @model_validator(mode="after")
    def validate_version_shape(self) -> CompiledPlanV1:
        if self.plan_version == 1 and self.predecessor_version is not None:
            raise ValueError("FIRST_PLAN_MUST_HAVE_NO_PREDECESSOR")
        if self.plan_version > 1 and self.predecessor_version is None:
            raise ValueError("REVISION_MUST_HAVE_PREDECESSOR")
        return self


def hypothesis_id(task_id: UUID, plan_version: int, source_local_id: str) -> UUID:
    if plan_version < 1 or LOCAL_ID_PATTERN.fullmatch(source_local_id) is None:
        raise ValueError("HYPOTHESIS_ID_INPUT_INVALID")
    return uuid5(HYPOTHESIS_NAMESPACE, f"{str(task_id).lower()}:{plan_version}:{source_local_id}")


def _item_candidate_refs(
    item_capability_requirements: tuple[CapabilityRequirementV1, ...],
    descriptors: tuple[CapabilityPlanningDescriptorV1, ...],
) -> tuple[CapabilityRefV1, ...]:
    refs: list[CapabilityRefV1] = []
    seen: set[CapabilityRefV1] = set()
    for requirement in item_capability_requirements:
        for descriptor in descriptors:
            if (
                descriptor.category == requirement.category
                and descriptor.capability_ref not in seen
            ):
                seen.add(descriptor.capability_ref)
                refs.append(descriptor.capability_ref)
    return tuple(refs)


def _inject_stop_conditions(deadline_at: datetime) -> tuple[StopConditionV1, ...]:
    return (
        StopConditionV1(
            stop_condition_id="stop-deadline",
            kind=StopConditionKindV1.DEADLINE,
            detail=f"stop by task deadline {deadline_at.isoformat()}",
        ),
        StopConditionV1(
            stop_condition_id="stop-no-progress",
            kind=StopConditionKindV1.NO_PROGRESS,
            detail=f"stop after {MAX_NO_PROGRESS_ROUNDS} consecutive no-progress rounds",
        ),
        StopConditionV1(
            stop_condition_id="stop-budget-exhausted",
            kind=StopConditionKindV1.BUDGET_EXHAUSTED,
            detail="stop when budget reservation is exhausted",
        ),
        StopConditionV1(
            stop_condition_id="stop-user-abort",
            kind=StopConditionKindV1.USER_ABORT,
            detail="stop on user abort",
        ),
        StopConditionV1(
            stop_condition_id="stop-conflict-unresolvable",
            kind=StopConditionKindV1.CONFLICT_UNRESOLVABLE,
            detail="stop when a required conflict is unresolvable",
        ),
    )


def compile_plan(
    *,
    task_id: UUID,
    plan_version: int,
    predecessor_version: int | None,
    input_id: UUID,
    draft: InitialPlanDraftV1,
    planning_view: CapabilityPlanningViewV1,
    deadline_at: datetime,
) -> CompiledPlanV1:
    """零LLM把`InitialPlanDraftV1`编译为`CompiledPlanV1`（仅首轮NEW_TASK）。"""

    if deadline_at.tzinfo is None or deadline_at.utcoffset() is None:
        raise ValueError("UTC_DATETIME_REQUIRED")
    deadline_at = deadline_at.astimezone(UTC)

    plan_item_ids = {
        item.local_id: plan_item_id(task_id, plan_version, item.local_id) for item in draft.items
    }
    ready_ids = set(draft.initial_ready_item_local_ids)

    items: list[CompiledPlanItemV1] = []
    for item in draft.items:
        compiled_item = CompiledPlanItemV1(
            plan_item_id=plan_item_ids[item.local_id],
            source_local_id=item.local_id,
            objective=item.objective,
            executor_requirement=item.executor_requirement,
            capability_requirements=item.capability_requirements,
            candidate_capability_refs=_item_candidate_refs(
                item.capability_requirements, planning_view.descriptors
            ),
            input_artifact_refs=item.input_artifact_refs,
            output_schema_ref=item.output_schema_ref,
            depends_on_item_ids=tuple(plan_item_ids[dep] for dep in item.depends_on_local_ids),
            iteration_policy=item.iteration_policy,
            initial_state="READY" if item.local_id in ready_ids else "OPEN",
            definition_digest="0" * 64,
        )
        items.append(
            compiled_item.model_copy(
                update={
                    "definition_digest": canonical_json_sha256(
                        compiled_item.model_dump(mode="json", exclude={"definition_digest"})
                    )
                }
            )
        )

    hypotheses: list[CompiledHypothesisV1] = []
    for hypothesis in draft.hypotheses:
        compiled_hypothesis = CompiledHypothesisV1(
            hypothesis_id=hypothesis_id(task_id, plan_version, hypothesis.local_id),
            source_local_id=hypothesis.local_id,
            statement=hypothesis.statement,
            initial_status="OPEN",
            hypothesis_digest="0" * 64,
        )
        hypotheses.append(
            compiled_hypothesis.model_copy(
                update={
                    "hypothesis_digest": canonical_json_sha256(
                        compiled_hypothesis.model_dump(mode="json", exclude={"hypothesis_digest"})
                    )
                }
            )
        )

    compiled_criteria: list[CompiledCriterionV1] = []
    for criterion in draft.success_criteria:
        source_kind, source_refs = derive_criterion_source(criterion)
        compiled_criteria.append(
            compile_criterion(
                task_id=task_id,
                plan_version=plan_version,
                draft=criterion,
                source_kind=source_kind,
                source_refs=source_refs,
                plan_item_ids=plan_item_ids,
            )
        )

    compiled_plan = CompiledPlanV1(
        contract_version="task_plan_version_v1",
        task_id=task_id,
        plan_version=plan_version,
        predecessor_version=predecessor_version,
        input_id=input_id,
        goal_understanding_ref=draft.goal_understanding_ref,
        hypotheses=tuple(hypotheses),
        items=tuple(items),
        compiled_criteria=tuple(compiled_criteria),
        stop_conditions=_inject_stop_conditions(deadline_at),
        initial_ready_item_ids=tuple(
            plan_item_ids[local_id] for local_id in draft.initial_ready_item_local_ids
        ),
        revision_reason="NEW_TASK",
        compiled_plan_digest="0" * 64,
    )
    digest = canonical_json_sha256(
        compiled_plan.model_dump(mode="json", exclude={"compiled_plan_digest"})
    )
    return compiled_plan.model_copy(update={"compiled_plan_digest": digest})


__all__ = [
    "CompiledHypothesisV1",
    "CompiledPlanItemV1",
    "CompiledPlanV1",
    "HYPOTHESIS_NAMESPACE",
    "MAX_NO_PROGRESS_ROUNDS",
    "StopConditionKindV1",
    "StopConditionV1",
    "compile_plan",
    "hypothesis_id",
]
