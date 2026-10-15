"""DEV-05 Goal/Plan激活编排与写集映射的离线验收。

以 `FakeSupervisorModel` + `FakeGoalPlanWriter` 驱动端到端切片：真实模型候选经确定性装配、
Gate、编译后投影为 `GoalCompletionWriteSet`/`PlanActivationWriteSet`。不接真实模型、不接 PG；
验证：映射确定性、READY_TO_PLAN 全量激活、REQUEST_USER_INPUT 澄清分支、非法引用/非 ACCEPT
校验的失败关闭。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from knowledge_system.infrastructure.persistence import (
    GoalCompletionWriteSet,
    PlanActivationWriteSet,
)
from knowledge_system.modules.tasking.application.errors import TaskEntryError
from knowledge_system.modules.tasking.application.supervisor_activation import (
    GoalActivationMaterials,
    PlanActivationMaterials,
    SupervisorActivationResult,
    SupervisorActivationService,
    build_goal_completion_write_set,
    build_plan_activation_write_set,
)
from knowledge_system.modules.tasking.domain.compiled_plan import compile_plan
from knowledge_system.modules.tasking.domain.core_types import (
    ArtifactRefV1,
    ConstraintRefV1,
    PolicySnapshotRefV1,
    ResourceDomainV1,
    ResourceRefV1,
)
from knowledge_system.modules.tasking.domain.goal_contracts import (
    GoalBlockingIssueV1,
    GoalBlockingReasonV1,
    GoalCriterionProposalV1,
    GoalUnderstandingContextV1,
    SafeAssumptionReasonV1,
    SafeAssumptionV1,
    SubgoalV1,
)
from knowledge_system.modules.tasking.domain.goal_gate import decide_goal_gate
from knowledge_system.modules.tasking.domain.input_contracts import (
    KnowledgeScopeModeV1,
    KnowledgeScopeV1,
    NetworkControlV1,
    NetworkPolicySourceV1,
    OutputContractV1,
    StructuredTaskControlsV1,
)
from knowledge_system.modules.tasking.domain.planning_fragments import (
    ArtifactCriterionTargetV1,
    CapabilityPlanningViewV1,
    CriterionDraftV1,
    HypothesisDraftV1,
    InitialPlanningContextV1,
    IterationPolicyV1,
    PlanItemDraftV1,
)
from knowledge_system.modules.tasking.domain.supervisor_candidates import (
    GoalUnderstandingModelOutputV1,
    InitialPlanModelOutputV1,
    assemble_goal_understanding,
    assemble_initial_plan,
)


def make_artifact_ref() -> ArtifactRefV1:
    return ArtifactRefV1(
        artifact_id=uuid4(),
        artifact_type="TEXT",
        sha256="a" * 64,
        schema_id="task_query_text",
        schema_version="v1",
    )


def make_resource_ref() -> ResourceRefV1:
    return ResourceRefV1(
        source_domain=ResourceDomainV1.TASK_ATTACHMENT,
        resource_id="res-1",
        version_id="v1",
    )


def make_constraint_ref() -> ConstraintRefV1:
    return ConstraintRefV1(constraint_id=uuid4(), input_id=uuid4(), parser_version="v1")


def make_policy_snapshot_ref() -> PolicySnapshotRefV1:
    return PolicySnapshotRefV1(
        policy_snapshot_id=uuid4(),
        policy_revision=1,
        policy_digest="b" * 64,
        observed_at=datetime.now(UTC),
    )


def make_structured_controls() -> StructuredTaskControlsV1:
    return StructuredTaskControlsV1(
        knowledge_scope=KnowledgeScopeV1(
            mode=KnowledgeScopeModeV1.DEFAULT_AUTHORIZED,
            directory_ids=(),
            knowledge_version_ids=(),
            include_personal=False,
        ),
        network_policy=NetworkControlV1(value="ASK", source=NetworkPolicySourceV1.SYSTEM_DEFAULT),
        output_contract=OutputContractV1(
            format="MARKDOWN",
            delivery="INLINE",
            language="zh-CN",
            schema_ref=None,
            template_or_skill_ref=None,
            citations="REQUIRED",
            max_output_bytes=10000,
        ),
        selected_resource_refs=(),
    )


def make_goal_context(
    *,
    authorized_resource_refs: tuple[ResourceRefV1, ...] = (),
) -> GoalUnderstandingContextV1:
    return GoalUnderstandingContextV1(
        contract_version="goal_understanding_context_v1",
        task_id=uuid4(),
        input_id=uuid4(),
        input_snapshot_digest="c" * 64,
        query_ref=make_artifact_ref(),
        structured_controls=make_structured_controls(),
        effective_constraint_refs=(),
        authorized_resource_refs=authorized_resource_refs,
        classifier_annotation_ref=None,
        policy_snapshot_ref=make_policy_snapshot_ref(),
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_profile="supervisor-goal-understanding-v1",
        schema_version="supervisor_goal_understanding_v1",
    )


def make_plan_context() -> InitialPlanningContextV1:
    return InitialPlanningContextV1(
        contract_version="supervisor_initial_plan_v1",
        task_id=uuid4(),
        input_id=uuid4(),
        goal_understanding_ref=make_artifact_ref(),
        effective_constraint_refs=(),
        output_contract_ref=make_artifact_ref(),
        resource_refs=(),
        context_pack_ref=make_artifact_ref(),
        capability_planning_view_ref=make_artifact_ref(),
        budget_reservation_ref=make_artifact_ref(),
        policy_snapshot_ref=make_policy_snapshot_ref(),
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_profile="supervisor-initial-plan-v1",
    )


def make_goal_output(
    *,
    resource_refs: tuple[ResourceRefV1, ...] = (),
    blocking_issues: tuple[GoalBlockingIssueV1, ...] = (),
) -> GoalUnderstandingModelOutputV1:
    return GoalUnderstandingModelOutputV1(
        contract_version="supervisor_goal_understanding_model_output_v1",
        primary_goal="完成本周工作目标梳理",
        expected_deliverable="一份Markdown报告",
        subgoals=(SubgoalV1(local_id="subgoal-1", statement="收集资料"),),
        resource_refs=resource_refs,
        success_criteria_proposals=(
            GoalCriterionProposalV1(
                local_id="sc-1", statement="交付物存在", evidence_kind="ARTIFACT"
            ),
        ),
        safe_assumptions=(
            SafeAssumptionV1(
                local_id="sa-1",
                statement="默认使用已授权资源",
                reason=SafeAssumptionReasonV1.REVERSIBLE_DEFAULT,
            ),
        ),
        blocking_issues=blocking_issues,
    )


def make_plan_output(*, goal_criterion_local_id: str = "sc-1") -> InitialPlanModelOutputV1:
    return InitialPlanModelOutputV1(
        contract_version="supervisor_initial_plan_model_output_v1",
        hypotheses=(HypothesisDraftV1(local_id="h-1", statement="假设资料充分"),),
        items=(
            PlanItemDraftV1(
                local_id="item-1",
                objective="生成报告",
                executor_requirement="SUPERVISOR",
                capability_requirements=(),
                input_artifact_refs=(),
                output_schema_ref="report",
                depends_on_local_ids=(),
                iteration_policy=IterationPolicyV1(
                    mode="SINGLE_PASS", max_iterations=1, stop_criterion_ids=()
                ),
            ),
        ),
        success_criteria=(
            CriterionDraftV1(
                local_id="crit-1",
                statement="报告已生成",
                goal_criterion_local_id=goal_criterion_local_id,
                target=ArtifactCriterionTargetV1(
                    kind="ARTIFACT", producer_item_local_id="item-1", output_schema_key="report"
                ),
            ),
        ),
        initial_ready_item_local_ids=("item-1",),
    )


def make_planning_view() -> CapabilityPlanningViewV1:
    return CapabilityPlanningViewV1(
        contract_version="capability_planning_view_v1",
        catalog_version="1",
        catalog_digest="d" * 64,
        descriptors=(),
    )


def make_goal_materials(task_id: UUID, input_id: UUID) -> GoalActivationMaterials:
    return GoalActivationMaterials(
        task_attempt_id=uuid4(),
        expected_task_version=1,
        goal_context_artifact_id=uuid4(),
        raw_model_artifact_id=uuid4(),
        goal_artifact_id=uuid4(),
        gate_artifact_id=uuid4(),
        now=datetime.now(UTC),
    )


def make_plan_materials() -> PlanActivationMaterials:
    return PlanActivationMaterials(
        task_attempt_id=uuid4(),
        expected_attempt_row_version=1,
        created_by_agent_instance_id=uuid4(),
        planning_context_artifact_id=uuid4(),
        capability_planning_view_artifact_id=uuid4(),
        raw_model_artifact_id=uuid4(),
        validated_draft_artifact_id=uuid4(),
        validation_artifact_id=uuid4(),
        compiled_plan_artifact_id=uuid4(),
        planning_view=make_planning_view(),
        budget_sufficient=True,
        now=datetime.now(UTC),
    )


class FakeSupervisorModel:
    def __init__(
        self,
        goal_output: GoalUnderstandingModelOutputV1 | None = None,
        plan_output: InitialPlanModelOutputV1 | None = None,
    ) -> None:
        self._goal_output = goal_output
        self._plan_output = plan_output

    async def generate_goal_understanding(
        self, context: GoalUnderstandingContextV1
    ) -> GoalUnderstandingModelOutputV1:
        assert self._goal_output is not None
        return self._goal_output

    async def generate_initial_plan(
        self, context: InitialPlanningContextV1
    ) -> InitialPlanModelOutputV1:
        assert self._plan_output is not None
        return self._plan_output


class FakeGoalPlanWriter:
    def __init__(self) -> None:
        self.goal_write_sets: list[GoalCompletionWriteSet] = []
        self.plan_write_sets: list[PlanActivationWriteSet] = []

    async def complete_goal_understanding(self, write_set: GoalCompletionWriteSet) -> None:
        self.goal_write_sets.append(write_set)

    async def activate_plan_version(self, write_set: PlanActivationWriteSet) -> None:
        self.plan_write_sets.append(write_set)


class GoalWriteSetMappingTests(unittest.TestCase):
    def test_goal_record_projects_deterministic_digests(self) -> None:
        context = make_goal_context()
        goal = assemble_goal_understanding(make_goal_output(), context)
        decision = decide_goal_gate(goal)
        materials = make_goal_materials(context.task_id, context.input_id)

        first = build_goal_completion_write_set(
            goal=goal, decision=decision, context=context, materials=materials
        )
        second = build_goal_completion_write_set(
            goal=goal, decision=decision, context=context, materials=materials
        )
        record = first.goal_understanding

        self.assertEqual(record.understanding_id, decision.understanding_id)
        self.assertEqual(record.understanding_digest, decision.understanding_digest)
        self.assertEqual(record.gate_outcome, "READY_TO_PLAN")
        self.assertEqual(record.subgoal_count, 1)
        self.assertEqual(record.criterion_count, 1)
        self.assertEqual(record.assumption_count, 1)
        self.assertEqual(record.blocking_issue_count, 0)
        self.assertEqual(record.task_id, context.task_id)
        self.assertEqual(record.input_id, context.input_id)
        self.assertEqual(record.gate_profile, "clarification_gate_v1")
        self.assertEqual(len(first.events), 1)
        self.assertEqual(first.events[0].event_type, "GOAL_UNDERSTANDING_COMPLETED")
        self.assertEqual(len(first.outbox_messages), 1)
        self.assertEqual(first.outbox_messages[0].destination, "MQ")

        # 内容寻址字段与计数重放稳定（事件/Outbox ID 为随机标识，不参与重放语义）。
        self.assertEqual(second.goal_understanding.understanding_id, record.understanding_id)
        self.assertEqual(
            second.goal_understanding.understanding_digest, record.understanding_digest
        )
        self.assertEqual(second.goal_understanding.gate_digest, record.gate_digest)
        self.assertEqual(second.goal_understanding.context_digest, record.context_digest)

    def test_goal_clarification_produces_request_and_two_events(self) -> None:
        context = make_goal_context()
        output = make_goal_output(
            blocking_issues=(
                GoalBlockingIssueV1(
                    local_id="bi-1",
                    reason=GoalBlockingReasonV1.GOAL_AMBIGUOUS,
                    field_path="/primary_goal",
                    description="目标歧义",
                    proposed_question_kind="SHORT_TEXT",
                    candidate_resource_refs=(),
                ),
            ),
        )
        goal = assemble_goal_understanding(output, context)
        decision = decide_goal_gate(goal)
        materials = make_goal_materials(context.task_id, context.input_id)

        write_set = build_goal_completion_write_set(
            goal=goal, decision=decision, context=context, materials=materials
        )

        self.assertEqual(write_set.goal_understanding.gate_outcome, "REQUEST_USER_INPUT")
        self.assertIsNotNone(write_set.user_input_request)
        request = write_set.user_input_request
        assert request is not None
        self.assertEqual(request.origin, "GOAL_CLARIFICATION")
        self.assertEqual(request.status, "PENDING")
        self.assertEqual(request.goal_understanding_id, decision.understanding_id)
        self.assertEqual(request.clarification_round_no, 1)
        self.assertEqual(request.expires_at - request.requested_at, timedelta(minutes=30))
        event_types = [event.event_type for event in write_set.events]
        self.assertEqual(event_types, ["GOAL_UNDERSTANDING_COMPLETED", "USER_INPUT_REQUESTED"])
        self.assertEqual(len(write_set.outbox_messages), 1)
        # Outbox 绑定澄清事件，而非完成事件。
        self.assertEqual(write_set.outbox_messages[0].event_id, write_set.events[1].id)


class PlanWriteSetMappingTests(unittest.TestCase):
    def test_plan_write_set_projects_compiled_definitions(self) -> None:
        context = make_plan_context()
        draft = assemble_initial_plan(make_plan_output(), context)
        compiled = compile_plan(
            task_id=context.task_id,
            plan_version=1,
            predecessor_version=None,
            input_id=context.input_id,
            draft=draft,
            planning_view=make_planning_view(),
            deadline_at=context.deadline_at,
        )
        materials = make_plan_materials()
        write_set = build_plan_activation_write_set(
            compiled=compiled, goal_understanding_id=uuid4(), materials=materials
        )

        self.assertEqual(write_set.plan_version.plan_version, 1)
        self.assertEqual(write_set.plan_version.status, "ACTIVE")
        self.assertEqual(write_set.plan_version.compiled_plan_digest, compiled.compiled_plan_digest)
        self.assertEqual(len(write_set.items), 1)
        self.assertEqual(write_set.items[0].source_local_id, "item-1")
        self.assertEqual(write_set.items[0].initial_state, "READY")
        self.assertEqual(len(write_set.hypotheses), 1)
        self.assertEqual(len(write_set.criteria), 1)
        self.assertEqual(write_set.criteria[0].source_kind, "GOAL")
        self.assertEqual(write_set.criteria[0].completion_role, "CORE_DELIVERABLE")
        self.assertEqual(len(write_set.runtime_rows), 1)
        self.assertEqual(write_set.runtime_rows[0].status, "READY")
        event_types = [event.event_type for event in write_set.events]
        self.assertEqual(event_types, ["PLAN_ACTIVATED", "PLAN_ITEM_READY"])
        self.assertEqual(write_set.events[0].business_ref_type, "TASK")
        self.assertEqual(write_set.events[1].business_ref_type, "PLAN_ITEM")
        self.assertEqual(len(write_set.outbox_messages), 1)
        assert write_set.attempt_completion is not None
        self.assertEqual(write_set.attempt_completion.result_kind, "SUCCEEDED")
        self.assertEqual(
            write_set.attempt_completion.result_digest, compiled.compiled_plan_digest
        )


class SupervisorActivationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_to_plan_activates_plan_end_to_end(self) -> None:
        goal_context = make_goal_context()
        plan_context = make_plan_context()
        model = FakeSupervisorModel(
            goal_output=make_goal_output(), plan_output=make_plan_output()
        )
        writer = FakeGoalPlanWriter()
        service = SupervisorActivationService(model, writer)

        result = await service.activate(
            goal_context,
            make_goal_materials(goal_context.task_id, goal_context.input_id),
            plan_context,
            make_plan_materials(),
        )

        self.assertIsInstance(result, SupervisorActivationResult)
        self.assertEqual(result.outcome, "PLAN_ACTIVATED")
        self.assertEqual(result.plan_version, 1)
        self.assertIsNotNone(result.compiled_plan_digest)
        self.assertEqual(len(writer.goal_write_sets), 1)
        self.assertEqual(len(writer.plan_write_sets), 1)
        self.assertEqual(writer.goal_write_sets[0].goal_understanding.gate_outcome, "READY_TO_PLAN")

    async def test_blocking_issue_returns_waiting_and_skips_plan(self) -> None:
        goal_context = make_goal_context()
        plan_context = make_plan_context()
        model = FakeSupervisorModel(
            goal_output=make_goal_output(
                blocking_issues=(
                    GoalBlockingIssueV1(
                        local_id="bi-1",
                        reason=GoalBlockingReasonV1.GOAL_AMBIGUOUS,
                        field_path="/primary_goal",
                        description="目标歧义",
                        proposed_question_kind="SHORT_TEXT",
                        candidate_resource_refs=(),
                    ),
                ),
            ),
            plan_output=make_plan_output(),
        )
        writer = FakeGoalPlanWriter()
        service = SupervisorActivationService(model, writer)

        result = await service.activate(
            goal_context,
            make_goal_materials(goal_context.task_id, goal_context.input_id),
            plan_context,
            make_plan_materials(),
        )

        self.assertEqual(result.outcome, "WAITING_USER_INPUT")
        self.assertEqual(result.plan_version, None)
        self.assertEqual(len(writer.goal_write_sets), 1)
        self.assertEqual(len(writer.plan_write_sets), 0)

    async def test_semantic_invalid_resource_ref_fails_closed(self) -> None:
        goal_context = make_goal_context(authorized_resource_refs=())
        plan_context = make_plan_context()
        model = FakeSupervisorModel(
            goal_output=make_goal_output(resource_refs=(make_resource_ref(),)),
            plan_output=make_plan_output(),
        )
        writer = FakeGoalPlanWriter()
        service = SupervisorActivationService(model, writer)

        with self.assertRaises(TaskEntryError) as caught:
            await service.activate(
                goal_context,
                make_goal_materials(goal_context.task_id, goal_context.input_id),
                plan_context,
                make_plan_materials(),
            )
        self.assertEqual(caught.exception.code, "GOAL_UNDERSTANDING_SEMANTIC_INVALID")
        self.assertEqual(len(writer.goal_write_sets), 0)
        self.assertEqual(len(writer.plan_write_sets), 0)

    async def test_plan_validation_rejection_fails_closed_after_goal_commit(self) -> None:
        goal_context = make_goal_context()
        plan_context = make_plan_context()
        # 计划成功条件不覆盖目标条件 → CRITERION_COVERAGE_MISSING → REPAIRABLE_INVALID。
        model = FakeSupervisorModel(
            goal_output=make_goal_output(),
            plan_output=make_plan_output(goal_criterion_local_id="x"),
        )
        writer = FakeGoalPlanWriter()
        service = SupervisorActivationService(model, writer)

        with self.assertRaises(TaskEntryError) as caught:
            await service.activate(
                goal_context,
                make_goal_materials(goal_context.task_id, goal_context.input_id),
                plan_context,
                make_plan_materials(),
            )
        self.assertEqual(caught.exception.code, "PLAN_VALIDATION_REPAIRABLE_INVALID")
        # Goal 已提交（先行的 Goal 步骤成功），Plan 未激活。
        self.assertEqual(len(writer.goal_write_sets), 1)
        self.assertEqual(len(writer.plan_write_sets), 0)


if __name__ == "__main__":
    unittest.main()
