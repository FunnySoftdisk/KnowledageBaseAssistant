"""DEV-05：Goal/Plan生成、编译与激活的端到端编排。

把「真实模型候选 → 确定性装配 → Goal Gate → 编译 → CAS激活」串成一条垂直切片：

- Goal 步骤产出 `TaskGoalUnderstandingRecord`（含澄清请求分支），经 `GoalPlanWriter` 提交；
- Plan 步骤仅在 Gate 为 `READY_TO_PLAN` 时产出 `TaskPlanVersionRecord` 与定义子表并原子激活。

映射函数（`build_goal_completion_write_set` / `build_plan_activation_write_set`）只做
领域对象 → 记录写集的确定性投影，供重放稳定；服务自身零 I/O、零 LLM，失败关闭（非法引用、
非 ACCEPT 校验、语义漂移均稳定拒绝）。写集事件序号/业务版本由 Repository 锁 Task 后回填，
本模块只填 `sequence=0` 与 `business_version=1` 占位。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol
from uuid import UUID, uuid4

from knowledge_system.infrastructure.persistence import (
    GoalCompletionWriteSet,
    PlanActivationWriteSet,
    PlanAttemptCompletion,
)
from knowledge_system.infrastructure.persistence.planning_models import (
    PlanItemRuntimeRecord,
    TaskCompiledCriterionRecord,
    TaskGoalUnderstandingRecord,
    TaskPlanHypothesisRecord,
    TaskPlanItemDefinitionRecord,
    TaskPlanVersionRecord,
)
from knowledge_system.infrastructure.persistence.task_models import (
    OutboxMessageRecord,
    TaskEventRecord,
    UserInputRequestRecord,
)
from knowledge_system.modules.tasking.domain.compiled_plan import CompiledPlanV1, compile_plan
from knowledge_system.modules.tasking.domain.core_types import canonical_json_sha256
from knowledge_system.modules.tasking.domain.goal_contracts import (
    GoalGateDecisionV1,
    GoalUnderstandingContextV1,
    GoalUnderstandingV1,
    validate_goal_relations,
)
from knowledge_system.modules.tasking.domain.goal_gate import (
    GOAL_GATE_PROFILE_DIGEST,
    decide_goal_gate,
)
from knowledge_system.modules.tasking.domain.plan_validation import validate_plan
from knowledge_system.modules.tasking.domain.planning_fragments import (
    CapabilityPlanningViewV1,
    InitialPlanningContextV1,
)
from knowledge_system.modules.tasking.domain.supervisor_candidates import (
    assemble_goal_understanding,
    assemble_initial_plan,
)
from knowledge_system.modules.tasking.domain.supervisor_profiles import (
    GOAL_UNDERSTANDING_PROFILE,
    supervisor_profile,
)

from .errors import TaskEntryError
from .supervisor_model import SupervisorModelPort

TASK_EVENT_SCHEMA_VERSION: Final = "task-event-v1"
OUTBOX_SCHEMA_VERSION: Final = "outbox-v1"
GOAL_CONTRACT_VERSION: Final = "supervisor_goal_understanding_v1"
GATE_PROFILE_DB: Final = "clarification_gate_v1"
MODEL_ALIAS: Final = "planning_strong"
RESPONSE_CONTRACT_VERSION: Final = "clarification_response_contract_v1"
CLARIFICATION_EXPIRY: Final = timedelta(minutes=30)
CLARIFICATION_DISPLAY_SUMMARY: Final = "需要补充信息"

_EMPTY_PAYLOAD_DIGEST: Final = canonical_json_sha256({})


@dataclass(frozen=True, slots=True)
class GoalActivationMaterials:
    """Goal 激活需要的权威材料：Attempt、Task 版本栅栏、产物 Artifact 与统一时钟。

    各 Artifact ID 由上层在候选生成前落库（内容寻址），本切片只投影引用；`now` 单一注入
    保证 Outbox `available_at` 与澄清请求时间戳的重放确定性。
    """

    task_attempt_id: UUID  # GOAL_UNDERSTANDING Attempt（须已 COMPLETED）
    expected_task_version: int
    goal_context_artifact_id: UUID
    raw_model_artifact_id: UUID
    goal_artifact_id: UUID
    gate_artifact_id: UUID
    now: datetime


@dataclass(frozen=True, slots=True)
class PlanActivationMaterials:
    """Plan 激活需要的权威材料：PLAN Attempt、Capability 视图与产物 Artifact。

    `planning_view`/`budget_sufficient`/`now` 是验证与编译的运行时输入；其余 Artifact ID 与
    `created_by_agent_instance_id` 在 Child 启动前已落库。
    """

    task_attempt_id: UUID  # PLAN Attempt（RUNNING，row_version=expected_attempt_row_version）
    expected_attempt_row_version: int
    created_by_agent_instance_id: UUID
    planning_context_artifact_id: UUID
    capability_planning_view_artifact_id: UUID
    raw_model_artifact_id: UUID
    validated_draft_artifact_id: UUID
    validation_artifact_id: UUID
    compiled_plan_artifact_id: UUID
    planning_view: CapabilityPlanningViewV1
    budget_sufficient: bool
    now: datetime


@dataclass(frozen=True, slots=True)
class SupervisorActivationResult:
    outcome: Literal["PLAN_ACTIVATED", "WAITING_USER_INPUT"]
    understanding_id: UUID
    plan_version: int | None = None
    compiled_plan_digest: str | None = None


class GoalPlanWriter(Protocol):
    """Goal/Plan 写集的提交端口；实现负责开事务、调用 Repository 并提交。"""

    async def complete_goal_understanding(self, write_set: GoalCompletionWriteSet) -> None: ...

    async def activate_plan_version(self, write_set: PlanActivationWriteSet) -> None: ...


def _build_outbox(task_id: UUID, event_id: UUID, now: datetime) -> OutboxMessageRecord:
    return OutboxMessageRecord(
        id=uuid4(),
        event_id=event_id,
        aggregate_type="TASK",
        aggregate_id=task_id,
        aggregate_version=1,
        destination="MQ",
        schema_version=OUTBOX_SCHEMA_VERSION,
        payload_json={},
        payload_digest=_EMPTY_PAYLOAD_DIGEST,
        publish_status="PENDING",
        available_at=now,
        attempt_count=0,
        row_version=1,
    )


def _goal_event(
    task_id: UUID, *, event_type: str, business_ref_type: str, business_ref_id: UUID
) -> TaskEventRecord:
    return TaskEventRecord(
        id=uuid4(),
        task_id=task_id,
        sequence=0,
        event_type=event_type,
        event_schema_version=TASK_EVENT_SCHEMA_VERSION,
        workflow_relevant=True,
        business_ref_type=business_ref_type,
        business_ref_id=business_ref_id,
        business_version=1,
        observation_expected_count=0,
        payload_json={},
        payload_digest=_EMPTY_PAYLOAD_DIGEST,
    )


def build_goal_completion_write_set(
    *,
    goal: GoalUnderstandingV1,
    decision: GoalGateDecisionV1,
    context: GoalUnderstandingContextV1,
    materials: GoalActivationMaterials,
) -> GoalCompletionWriteSet:
    """把装配后的 Goal 与确定性 Gate 决定投影为原子提交写集（含澄清分支）。"""

    profile = supervisor_profile(GOAL_UNDERSTANDING_PROFILE)
    record = TaskGoalUnderstandingRecord(
        understanding_id=decision.understanding_id,
        task_id=context.task_id,
        input_id=context.input_id,
        task_attempt_id=materials.task_attempt_id,
        contract_version=GOAL_CONTRACT_VERSION,
        prompt_profile=GOAL_UNDERSTANDING_PROFILE,
        prompt_profile_digest=profile.profile_digest,
        model_alias=MODEL_ALIAS,
        goal_context_artifact_id=materials.goal_context_artifact_id,
        context_digest=canonical_json_sha256(context.model_dump(mode="json")),
        raw_model_artifact_id=materials.raw_model_artifact_id,
        goal_artifact_id=materials.goal_artifact_id,
        understanding_digest=decision.understanding_digest,
        gate_profile=GATE_PROFILE_DB,
        gate_profile_digest=GOAL_GATE_PROFILE_DIGEST,
        gate_artifact_id=materials.gate_artifact_id,
        gate_digest=canonical_json_sha256(decision.model_dump(mode="json")),
        gate_outcome=decision.outcome,
        subgoal_count=len(goal.subgoals),
        criterion_count=len(goal.success_criteria_proposals),
        assumption_count=len(goal.safe_assumptions),
        blocking_issue_count=len(goal.blocking_issues),
    )
    completion_event = _goal_event(
        context.task_id,
        event_type="GOAL_UNDERSTANDING_COMPLETED",
        business_ref_type="GOAL",
        business_ref_id=decision.understanding_id,
    )
    if decision.outcome == "REQUEST_USER_INPUT":
        request = UserInputRequestRecord(
            id=uuid4(),
            task_id=context.task_id,
            input_id=context.input_id,
            origin="GOAL_CLARIFICATION",
            source_task_attempt_id=materials.task_attempt_id,
            question_artifact_id=materials.gate_artifact_id,
            input_schema_json={},
            response_contract_version=RESPONSE_CONTRACT_VERSION,
            display_summary=CLARIFICATION_DISPLAY_SUMMARY,
            status="PENDING",
            response_artifact_id=None,
            response_size_bytes=None,
            goal_understanding_id=decision.understanding_id,
            clarification_round_no=1,
            resume_bundle_id=None,
            deferred_call_id=None,
            runtime_blocking_issue_id=None,
            issue_fingerprint=None,
            request_version=1,
            row_version=1,
            requested_at=materials.now,
            expires_at=materials.now + CLARIFICATION_EXPIRY,
            submitted_at=None,
        )
        request_event = _goal_event(
            context.task_id,
            event_type="USER_INPUT_REQUESTED",
            business_ref_type="USER_INPUT_REQUEST",
            business_ref_id=request.id,
        )
        events: tuple[TaskEventRecord, ...] = (completion_event, request_event)
        outbox = (_build_outbox(context.task_id, request_event.id, materials.now),)
    else:
        request = None
        events = (completion_event,)
        outbox = (_build_outbox(context.task_id, completion_event.id, materials.now),)
    return GoalCompletionWriteSet(
        goal_understanding=record,
        expected_task_version=materials.expected_task_version,
        user_input_request=request,
        events=events,
        outbox_messages=outbox,
    )


def build_plan_activation_write_set(
    *,
    compiled: CompiledPlanV1,
    goal_understanding_id: UUID,
    materials: PlanActivationMaterials,
) -> PlanActivationWriteSet:
    """把已编译 Plan 投影为原子激活写集（定义子表 + 运行投影 + 事件 + Outbox）。"""

    plan_version = TaskPlanVersionRecord(
        task_id=compiled.task_id,
        plan_version=compiled.plan_version,
        predecessor_version=compiled.predecessor_version,
        input_id=compiled.input_id,
        task_attempt_id=materials.task_attempt_id,
        goal_understanding_id=goal_understanding_id,
        planning_context_artifact_id=materials.planning_context_artifact_id,
        capability_planning_view_artifact_id=materials.capability_planning_view_artifact_id,
        raw_model_artifact_id=materials.raw_model_artifact_id,
        validated_draft_artifact_id=materials.validated_draft_artifact_id,
        validation_artifact_id=materials.validation_artifact_id,
        compiled_plan_artifact_id=materials.compiled_plan_artifact_id,
        compiled_plan_digest=compiled.compiled_plan_digest,
        revision_reason=compiled.revision_reason,
        status="ACTIVE",
        created_by_agent_instance_id=materials.created_by_agent_instance_id,
        activated_at=materials.now,
        completed_at=None,
    )
    items = tuple(
        TaskPlanItemDefinitionRecord(
            task_id=compiled.task_id,
            plan_version=compiled.plan_version,
            plan_item_id=item.plan_item_id,
            source_local_id=item.source_local_id,
            objective=item.objective,
            executor_requirement=item.executor_requirement,
            capability_requirements_json=[
                requirement.model_dump(mode="json") for requirement in item.capability_requirements
            ],
            candidate_capability_refs_json=[
                ref.model_dump(mode="json") for ref in item.candidate_capability_refs
            ],
            input_artifact_refs_json=[
                ref.model_dump(mode="json") for ref in item.input_artifact_refs
            ],
            output_schema_ref_json={"key": item.output_schema_ref},
            depends_on_item_ids_json=[str(dep) for dep in item.depends_on_item_ids],
            iteration_policy_json=item.iteration_policy.model_dump(mode="json"),
            initial_state=item.initial_state,
            definition_digest=item.definition_digest,
        )
        for item in compiled.items
    )
    hypotheses = tuple(
        TaskPlanHypothesisRecord(
            task_id=compiled.task_id,
            plan_version=compiled.plan_version,
            hypothesis_id=hypothesis.hypothesis_id,
            source_local_id=hypothesis.source_local_id,
            statement=hypothesis.statement,
            initial_status=hypothesis.initial_status,
            hypothesis_digest=hypothesis.hypothesis_digest,
        )
        for hypothesis in compiled.hypotheses
    )
    criteria = tuple(
        TaskCompiledCriterionRecord(
            task_id=compiled.task_id,
            plan_version=compiled.plan_version,
            criterion_id=criterion.criterion_id,
            statement=criterion.statement,
            source_kind=criterion.source_kind.value,
            source_refs=list(criterion.source_refs),
            requirement_level=criterion.requirement_level,
            completion_role=criterion.completion_role,
            protected=criterion.protected,
            evaluation_mode=criterion.evaluation_mode,
            predicate=criterion.predicate.model_dump(mode="json"),
            applicability=(
                {"keys": list(criterion.applicability)}
                if criterion.applicability is not None
                else None
            ),
            evaluator_profile={"profile": criterion.evaluator_profile},
            digest=criterion.digest,
        )
        for criterion in compiled.compiled_criteria
    )
    runtime_rows = tuple(
        PlanItemRuntimeRecord(
            task_id=compiled.task_id,
            plan_version=compiled.plan_version,
            plan_item_id=item.plan_item_id,
            status=item.initial_state,
            current_iteration=0,
            active_task_attempt_id=None,
            last_error_code=None,
            row_version=1,
            started_at=None,
            completed_at=None,
        )
        for item in compiled.items
    )
    activated_event = _goal_event(
        compiled.task_id,
        event_type="PLAN_ACTIVATED",
        business_ref_type="TASK",
        business_ref_id=compiled.task_id,
    )
    ready_events = tuple(
        _goal_event(
            compiled.task_id,
            event_type="PLAN_ITEM_READY",
            business_ref_type="PLAN_ITEM",
            business_ref_id=item.plan_item_id,
        )
        for item in compiled.items
        if item.initial_state == "READY"
    )
    outbox = (_build_outbox(compiled.task_id, activated_event.id, materials.now),)
    return PlanActivationWriteSet(
        plan_version=plan_version,
        attempt_completion=PlanAttemptCompletion(
            task_attempt_id=materials.task_attempt_id,
            expected_row_version=materials.expected_attempt_row_version,
            result_kind="SUCCEEDED",
            result_schema_version="attempt_completed_v1",
            result_digest=compiled.compiled_plan_digest,
            ended_at=materials.now,
        ),
        goal_understanding=None,
        items=items,
        hypotheses=hypotheses,
        criteria=criteria,
        runtime_rows=runtime_rows,
        events=(activated_event, *ready_events),
        outbox_messages=outbox,
    )


class SupervisorActivationService:
    """Goal→Plan 的垂直编排：模型候选 → 装配 → 校验 → Gate → 编译 → 激活。"""

    def __init__(self, model: SupervisorModelPort, writer: GoalPlanWriter) -> None:
        self._model = model
        self._writer = writer

    async def activate(
        self,
        goal_context: GoalUnderstandingContextV1,
        goal_materials: GoalActivationMaterials,
        plan_context: InitialPlanningContextV1,
        plan_materials: PlanActivationMaterials,
    ) -> SupervisorActivationResult:
        goal_output = await self._model.generate_goal_understanding(goal_context)
        goal = assemble_goal_understanding(goal_output, goal_context)
        if validate_goal_relations(goal, goal_context):
            raise TaskEntryError("GOAL_UNDERSTANDING_SEMANTIC_INVALID")
        decision = decide_goal_gate(goal)
        await self._writer.complete_goal_understanding(
            build_goal_completion_write_set(
                goal=goal, decision=decision, context=goal_context, materials=goal_materials
            )
        )
        if decision.outcome == "REQUEST_USER_INPUT":
            return SupervisorActivationResult(
                outcome="WAITING_USER_INPUT", understanding_id=decision.understanding_id
            )

        plan_output = await self._model.generate_initial_plan(plan_context)
        draft = assemble_initial_plan(plan_output, plan_context)
        goal_criterion_ids = {criterion.local_id for criterion in goal.success_criteria_proposals}
        validation = validate_plan(
            task_id=plan_context.task_id,
            plan_version=1,
            predecessor_version=None,
            input_id=plan_context.input_id,
            draft=draft,
            goal_criterion_ids=goal_criterion_ids,
            planning_view=plan_materials.planning_view,
            deadline_at=plan_context.deadline_at,
            now=plan_materials.now,
            budget_sufficient=plan_materials.budget_sufficient,
        )
        if validation.outcome != "ACCEPT":
            raise TaskEntryError(f"PLAN_VALIDATION_{validation.outcome}")
        compiled = compile_plan(
            task_id=plan_context.task_id,
            plan_version=1,
            predecessor_version=None,
            input_id=plan_context.input_id,
            draft=draft,
            planning_view=plan_materials.planning_view,
            deadline_at=plan_context.deadline_at,
        )
        await self._writer.activate_plan_version(
            build_plan_activation_write_set(
                compiled=compiled,
                goal_understanding_id=decision.understanding_id,
                materials=plan_materials,
            )
        )
        return SupervisorActivationResult(
            outcome="PLAN_ACTIVATED",
            understanding_id=decision.understanding_id,
            plan_version=compiled.plan_version,
            compiled_plan_digest=compiled.compiled_plan_digest,
        )


__all__ = [
    "GoalActivationMaterials",
    "GoalPlanWriter",
    "PlanActivationMaterials",
    "SupervisorActivationResult",
    "SupervisorActivationService",
    "build_goal_completion_write_set",
    "build_plan_activation_write_set",
]
