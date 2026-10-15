"""DEV-05 Supervisor模型候选生成：候选DTO、装配、Goal Gate、Profile与PydanticAI适配器。

纯离线验收：模型端口以`FunctionModel`/Fake替身驱动，不接真实模型、不接PG/Temporal；
结构修复（≤2次）以PydanticAI `retries`验证，超出预算失败关闭不静默放宽Schema。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import ValidationError
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from knowledge_system.infrastructure.supervisor_model import PydanticSupervisorModel
from knowledge_system.modules.tasking.application.errors import TaskEntryError
from knowledge_system.modules.tasking.application.supervisor_planning import (
    GoalUnderstandingService,
    InitialPlanService,
)
from knowledge_system.modules.tasking.domain.core_types import (
    ArtifactRefV1,
    ConstraintRefV1,
    PolicySnapshotRefV1,
    ResourceDomainV1,
    ResourceRefV1,
    canonical_json_sha256,
)
from knowledge_system.modules.tasking.domain.goal_contracts import (
    GoalBlockingIssueV1,
    GoalBlockingReasonV1,
    GoalCriterionProposalV1,
    GoalGateDecisionV1,
    GoalUnderstandingContextV1,
    SafeAssumptionReasonV1,
    SafeAssumptionV1,
    SubgoalV1,
    validate_goal_relations,
)
from knowledge_system.modules.tasking.domain.goal_gate import (
    GOAL_GATE_PROFILE_DIGEST,
    decide_goal_gate,
)
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
    CriterionDraftV1,
    HypothesisDraftV1,
    InitialPlanDraftV1,
    InitialPlanningContextV1,
    IterationPolicyV1,
    PlanItemDraftV1,
)
from knowledge_system.modules.tasking.domain.supervisor_candidates import (
    GoalUnderstandingModelOutputV1,
    InitialPlanModelOutputV1,
    assemble_goal_understanding,
    assemble_initial_plan,
    goal_understanding_id,
)
from knowledge_system.modules.tasking.domain.supervisor_profiles import (
    GOAL_UNDERSTANDING_PROFILE,
    INITIAL_PLAN_PROFILE,
    STRUCTURAL_REPAIR_LIMIT,
    supervisor_profile,
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
        network_policy=NetworkControlV1(
            value="ASK", source=NetworkPolicySourceV1.SYSTEM_DEFAULT
        ),
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
    effective_constraint_refs: tuple[ConstraintRefV1, ...] = (),
) -> GoalUnderstandingContextV1:
    return GoalUnderstandingContextV1(
        contract_version="goal_understanding_context_v1",
        task_id=uuid4(),
        input_id=uuid4(),
        input_snapshot_digest="c" * 64,
        query_ref=make_artifact_ref(),
        structured_controls=make_structured_controls(),
        effective_constraint_refs=effective_constraint_refs,
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


def make_plan_output() -> InitialPlanModelOutputV1:
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
                goal_criterion_local_id="sc-1",
                target=ArtifactCriterionTargetV1(
                    kind="ARTIFACT", producer_item_local_id="item-1", output_schema_key="report"
                ),
            ),
        ),
        initial_ready_item_local_ids=("item-1",),
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


class SupervisorProfileTests(unittest.TestCase):
    def test_two_profiles_registered_with_repair_limit(self) -> None:
        for name in (GOAL_UNDERSTANDING_PROFILE, INITIAL_PLAN_PROFILE):
            with self.subTest(name=name):
                profile = supervisor_profile(name)
                self.assertEqual(profile.profile_name, name)
                self.assertEqual(profile.structural_repair_limit, 2)
                self.assertEqual(profile.structural_repair_limit, STRUCTURAL_REPAIR_LIMIT)

    def test_profile_digests_are_deterministic(self) -> None:
        first = supervisor_profile(GOAL_UNDERSTANDING_PROFILE)
        second = supervisor_profile(GOAL_UNDERSTANDING_PROFILE)
        self.assertEqual(first.schema_digest, second.schema_digest)
        self.assertEqual(first.profile_digest, second.profile_digest)
        self.assertNotEqual(
            supervisor_profile(GOAL_UNDERSTANDING_PROFILE).schema_digest,
            supervisor_profile(INITIAL_PLAN_PROFILE).schema_digest,
        )

    def test_unknown_profile_fails_closed(self) -> None:
        with self.assertRaises(ValueError) as caught:
            supervisor_profile("supervisor-unknown-v1")
        self.assertEqual(str(caught.exception), "SUPERVISOR_PROFILE_NOT_REGISTERED")


class SupervisorCandidateTests(unittest.TestCase):
    def test_goal_output_rejects_duplicate_local_ids(self) -> None:
        with self.assertRaises(ValidationError):
            GoalUnderstandingModelOutputV1(
                contract_version="supervisor_goal_understanding_model_output_v1",
                primary_goal="g",
                expected_deliverable="d",
                subgoals=(
                    SubgoalV1(local_id="dup", statement="a"),
                    SubgoalV1(local_id="dup", statement="b"),
                ),
                resource_refs=(),
                success_criteria_proposals=(
                    GoalCriterionProposalV1(
                        local_id="sc-1", statement="s", evidence_kind="ARTIFACT"
                    ),
                ),
                safe_assumptions=(),
                blocking_issues=(),
            )

    def test_goal_output_requires_at_least_one_success_criterion(self) -> None:
        with self.assertRaises(ValidationError):
            GoalUnderstandingModelOutputV1(
                contract_version="supervisor_goal_understanding_model_output_v1",
                primary_goal="g",
                expected_deliverable="d",
                subgoals=(),
                resource_refs=(),
                success_criteria_proposals=(),
                safe_assumptions=(),
                blocking_issues=(),
            )

    def test_plan_output_requires_at_least_one_item_and_criterion(self) -> None:
        with self.assertRaises(ValidationError):
            InitialPlanModelOutputV1(
                contract_version="supervisor_initial_plan_model_output_v1",
                hypotheses=(),
                items=(),
                success_criteria=(),
                initial_ready_item_local_ids=("item-1",),
            )


class AssemblyTests(unittest.TestCase):
    def test_assemble_goal_injects_server_fields(self) -> None:
        context = make_goal_context(
            authorized_resource_refs=(make_resource_ref(),),
            effective_constraint_refs=(make_constraint_ref(),),
        )
        resource_ref = context.authorized_resource_refs[0]
        output = make_goal_output(resource_refs=(resource_ref,))
        goal = assemble_goal_understanding(output, context)
        self.assertEqual(goal.input_id, context.input_id)
        self.assertEqual(goal.effective_constraint_refs, context.effective_constraint_refs)
        self.assertEqual(goal.primary_goal, output.primary_goal)
        self.assertEqual(goal.resource_refs, (resource_ref,))
        self.assertFalse(validate_goal_relations(goal, context))

    def test_assemble_plan_injects_goal_ref_and_profile(self) -> None:
        context = make_plan_context()
        draft = assemble_initial_plan(make_plan_output(), context)
        self.assertEqual(draft.goal_understanding_ref, context.goal_understanding_ref)
        self.assertEqual(draft.prompt_profile, "supervisor-initial-plan-v1")
        self.assertEqual(draft.items[0].local_id, "item-1")

    def test_goal_understanding_id_is_content_addressed(self) -> None:
        context = make_goal_context()
        goal = assemble_goal_understanding(make_goal_output(), context)
        digest = canonical_json_sha256(goal.model_dump(mode="json"))
        expected = goal_understanding_id(digest)
        self.assertEqual(goal_understanding_id(digest), expected)
        self.assertEqual(goal_understanding_id(digest).version, 5)
        # 同一digest重复派生稳定
        self.assertEqual(str(goal_understanding_id(digest)), str(expected))


class GoalGateTests(unittest.TestCase):
    def test_ready_to_plan_accepts_all_assumptions(self) -> None:
        context = make_goal_context()
        goal = assemble_goal_understanding(make_goal_output(), context)
        decision = decide_goal_gate(goal)
        self.assertEqual(decision.outcome, "READY_TO_PLAN")
        self.assertEqual(decision.accepted_assumption_ids, ("sa-1",))
        self.assertEqual(decision.reason_codes, ())
        self.assertEqual(decision.gate_profile_digest, GOAL_GATE_PROFILE_DIGEST)

    def test_blocking_issue_requests_user_input(self) -> None:
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
        self.assertEqual(decision.outcome, "REQUEST_USER_INPUT")
        self.assertEqual(decision.validated_issue_ids, ("bi-1",))
        self.assertEqual(decision.reason_codes, (GoalBlockingReasonV1.GOAL_AMBIGUOUS,))
        self.assertEqual(decision.accepted_assumption_ids, ())


class GoalUnderstandingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_understand_ready_to_plan(self) -> None:
        context = make_goal_context(
            authorized_resource_refs=(make_resource_ref(),),
        )
        resource_ref = context.authorized_resource_refs[0]
        service = GoalUnderstandingService(
            FakeSupervisorModel(goal_output=make_goal_output(resource_refs=(resource_ref,)))
        )
        decision = await service.understand(context)
        self.assertIsInstance(decision, GoalGateDecisionV1)
        self.assertEqual(decision.outcome, "READY_TO_PLAN")

    async def test_understand_semantic_invalid_resource_ref_fails_closed(self) -> None:
        context = make_goal_context(authorized_resource_refs=())
        # 模型提出未授权资源引用 → 语义校验失败关闭
        service = GoalUnderstandingService(
            FakeSupervisorModel(
                goal_output=make_goal_output(resource_refs=(make_resource_ref(),))
            )
        )
        with self.assertRaises(TaskEntryError) as caught:
            await service.understand(context)
        self.assertEqual(caught.exception.code, "GOAL_UNDERSTANDING_SEMANTIC_INVALID")


class InitialPlanServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_draft_assembles_plan(self) -> None:
        context = make_plan_context()
        service = InitialPlanService(FakeSupervisorModel(plan_output=make_plan_output()))
        draft = await service.draft(context)
        self.assertIsInstance(draft, InitialPlanDraftV1)
        self.assertEqual(draft.goal_understanding_ref, context.goal_understanding_ref)


class PydanticSupervisorModelTests(unittest.IsolatedAsyncioTestCase):
    def _responder(self, payload: str):
        async def respond(
            messages: list[ModelMessage], info: AgentInfo
        ) -> ModelResponse:
            self.assertTrue(messages)
            self.assertEqual(info.function_tools, [])
            return ModelResponse([TextPart(payload)])

        return respond

    async def test_goal_understanding_round_trip(self) -> None:
        context = make_goal_context()
        expected = make_goal_output()
        model = PydanticSupervisorModel(FunctionModel(self._responder(expected.model_dump_json())))
        output = await model.generate_goal_understanding(context)
        self.assertEqual(output, expected)

    async def test_initial_plan_round_trip(self) -> None:
        context = make_plan_context()
        expected = make_plan_output()
        model = PydanticSupervisorModel(FunctionModel(self._responder(expected.model_dump_json())))
        output = await model.generate_initial_plan(context)
        self.assertEqual(output, expected)

    async def test_structural_repair_recovers_within_budget(self) -> None:
        context = make_goal_context()
        valid = make_goal_output()
        calls = 0

        async def respond(
            messages: list[ModelMessage], info: AgentInfo
        ) -> ModelResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                return ModelResponse([TextPart('{"not":"valid"}')])
            return ModelResponse([TextPart(valid.model_dump_json())])

        model = PydanticSupervisorModel(FunctionModel(respond))
        output = await model.generate_goal_understanding(context)
        self.assertEqual(output, valid)
        self.assertEqual(calls, 2)

    async def test_structural_repair_exhaustion_fails_closed(self) -> None:
        context = make_goal_context()
        calls = 0

        async def respond(
            messages: list[ModelMessage], info: AgentInfo
        ) -> ModelResponse:
            nonlocal calls
            calls += 1
            return ModelResponse([TextPart('{"primary_goal":123}')])

        model = PydanticSupervisorModel(FunctionModel(respond))
        with self.assertRaises(UnexpectedModelBehavior):
            await model.generate_goal_understanding(context)
        self.assertEqual(calls, STRUCTURAL_REPAIR_LIMIT + 1)

    async def test_adapter_satisfies_port_in_service(self) -> None:
        context = make_goal_context(authorized_resource_refs=(make_resource_ref(),))
        resource_ref = context.authorized_resource_refs[0]
        expected = make_goal_output(resource_refs=(resource_ref,))
        model = PydanticSupervisorModel(
            FunctionModel(self._responder(expected.model_dump_json()))
        )
        service = GoalUnderstandingService(model)
        decision = await service.understand(context)
        self.assertEqual(decision.outcome, "READY_TO_PLAN")


if __name__ == "__main__":
    unittest.main()
