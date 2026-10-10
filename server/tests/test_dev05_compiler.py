"""DEV-05：Criterion/Plan Compiler与Validator的离线验收；不证明真实模型/PG激活。"""

import unittest
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError

from knowledge_system.modules.tasking.domain.compiled_criterion import (
    ArtifactContractPredicateV1,
    CompiledCriterionV1,
    ConstraintPredicateV1,
    EvidenceSupportPredicateV1,
    StateFactPredicateV1,
    compile_criterion,
    derive_criterion_source,
)
from knowledge_system.modules.tasking.domain.compiled_plan import (
    HYPOTHESIS_NAMESPACE,
    CompiledPlanV1,
    StopConditionKindV1,
    compile_plan,
    hypothesis_id,
)
from knowledge_system.modules.tasking.domain.core_types import (
    ArtifactRefV1,
    CapabilityRefV1,
    ConstraintRefV1,
)
from knowledge_system.modules.tasking.domain.plan_validation import validate_plan
from knowledge_system.modules.tasking.domain.planning_fragments import (
    ArtifactCriterionTargetV1,
    CapabilityPlanningDescriptorV1,
    CapabilityPlanningViewV1,
    CapabilityRequirementV1,
    CitationCriterionTargetV1,
    CriterionDraftV1,
    CriterionSourceKindV1,
    HypothesisDraftV1,
    InitialPlanDraftV1,
    InitialPlanValidationReasonV1,
    IterationPolicyV1,
    PlanItemDraftV1,
    StateCriterionTargetV1,
    UserCriterionTargetV1,
    plan_item_id,
)

ZERO_SHA = "0" * 64
TASK_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
INPUT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
CONSTRAINT_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
ARTIFACT_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")


def artifact_ref() -> ArtifactRefV1:
    return ArtifactRefV1(
        artifact_id=ARTIFACT_ID,
        artifact_type="GOAL_UNDERSTANDING",
        sha256=ZERO_SHA,
        schema_id="goal-understanding",
        schema_version="1",
    )


def constraint_ref() -> ConstraintRefV1:
    return ConstraintRefV1(
        constraint_id=CONSTRAINT_ID, input_id=INPUT_ID, parser_version="1"
    )


def valid_draft() -> InitialPlanDraftV1:
    return InitialPlanDraftV1(
        contract_version="supervisor_initial_plan_v1",
        goal_understanding_ref=artifact_ref(),
        hypotheses=(HypothesisDraftV1(local_id="h1", statement="assumption"),),
        items=(
            PlanItemDraftV1(
                local_id="item1",
                objective="write recommendation",
                executor_requirement="SPECIALIST",
                capability_requirements=(
                    CapabilityRequirementV1(
                        category="writing",
                        required_input_kinds=("USER_FACT",),
                        required_output_kind="RECOMMENDATION",
                    ),
                ),
                input_artifact_refs=(),
                output_schema_ref="recommendation-v1@1",
                depends_on_local_ids=(),
                iteration_policy=IterationPolicyV1(
                    mode="SINGLE_PASS", max_iterations=1, stop_criterion_ids=()
                ),
            ),
        ),
        success_criteria=(
            CriterionDraftV1(
                local_id="criterion1",
                statement="recommendation artifact exists",
                goal_criterion_local_id="goalcriterion1",
                target=ArtifactCriterionTargetV1(
                    kind="ARTIFACT",
                    producer_item_local_id="item1",
                    output_schema_key="recommendation-v1@1",
                ),
            ),
        ),
        initial_ready_item_local_ids=("item1",),
        prompt_profile="supervisor-initial-plan-v1",
    )


def planning_view() -> CapabilityPlanningViewV1:
    return CapabilityPlanningViewV1(
        contract_version="capability_planning_view_v1",
        catalog_version="1",
        catalog_digest=ZERO_SHA,
        descriptors=(
            CapabilityPlanningDescriptorV1(
                capability_ref=CapabilityRefV1(
                    capability_id="writing-v1",
                    capability_version="1",
                    integrity_digest=ZERO_SHA,
                ),
                category="writing",
                safe_summary="produce recommendations",
                input_schema_key="user-fact-v1@1",
                output_schema_key="recommendation-v1@1",
                allowed_executor_kinds=("BUILT_IN",),
            ),
        ),
    )


def future_deadline() -> datetime:
    return datetime(2026, 10, 2, 0, 0, 0, tzinfo=UTC)


def now() -> datetime:
    return datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC)


def item_ids() -> dict[str, UUID]:
    return {"item1": plan_item_id(TASK_ID, 1, "item1")}


class CriterionCompilerTests(unittest.TestCase):
    def test_artifact_maps_to_core_deliverable_contract(self) -> None:
        draft = valid_draft().success_criteria[0]
        compiled = compile_criterion(
            task_id=TASK_ID,
            plan_version=1,
            draft=draft,
            source_kind=CriterionSourceKindV1.GOAL,
            source_refs=("goalcriterion1",),
            plan_item_ids=item_ids(),
        )
        self.assertEqual(compiled.completion_role, "CORE_DELIVERABLE")
        self.assertEqual(compiled.requirement_level, "REQUIRED")
        self.assertEqual(compiled.protected, True)
        self.assertEqual(compiled.evaluation_mode, "DETERMINISTIC")
        self.assertIsInstance(compiled.predicate, ArtifactContractPredicateV1)
        self.assertEqual(compiled.predicate.producer_plan_item_id, item_ids()["item1"])
        self.assertEqual(compiled.predicate.output_schema_key, "recommendation-v1@1")

    def test_citation_maps_to_evidence_support(self) -> None:
        draft = CriterionDraftV1(
            local_id="c1",
            statement="must cite sources",
            goal_criterion_local_id=None,
            target=CitationCriterionTargetV1(
                kind="CITATION", producer_item_local_id="item1", min_citations=2
            ),
        )
        compiled = compile_criterion(
            task_id=TASK_ID,
            plan_version=1,
            draft=draft,
            source_kind=CriterionSourceKindV1.PLAN,
            source_refs=("c1",),
            plan_item_ids=item_ids(),
        )
        self.assertIsInstance(compiled.predicate, EvidenceSupportPredicateV1)
        self.assertEqual(compiled.predicate.min_citations, 2)
        self.assertEqual(compiled.evaluation_mode, "EVIDENCE_GROUNDED_SEMANTIC")
        self.assertEqual(compiled.completion_role, "REQUIRED_AUXILIARY")
        self.assertEqual(compiled.protected, False)

    def test_state_maps_to_state_fact(self) -> None:
        draft = CriterionDraftV1(
            local_id="c2",
            statement="item completed",
            goal_criterion_local_id=None,
            target=StateCriterionTargetV1(
                kind="STATE", subject_item_local_id="item1", expected_state="COMPLETED"
            ),
        )
        compiled = compile_criterion(
            task_id=TASK_ID,
            plan_version=1,
            draft=draft,
            source_kind=CriterionSourceKindV1.PLAN,
            source_refs=("c2",),
            plan_item_ids=item_ids(),
        )
        self.assertIsInstance(compiled.predicate, StateFactPredicateV1)
        self.assertEqual(compiled.predicate.subject_plan_item_id, item_ids()["item1"])

    def test_user_maps_to_constraint_and_is_protected(self) -> None:
        draft = CriterionDraftV1(
            local_id="c3",
            statement="respect constraint",
            goal_criterion_local_id=None,
            target=UserCriterionTargetV1(kind="USER", constraint_ref=constraint_ref()),
        )
        source_kind, source_refs = derive_criterion_source(draft)
        self.assertEqual(source_kind, CriterionSourceKindV1.USER)
        self.assertEqual(source_refs, (str(CONSTRAINT_ID).lower(),))
        compiled = compile_criterion(
            task_id=TASK_ID,
            plan_version=1,
            draft=draft,
            source_kind=source_kind,
            source_refs=source_refs,
            plan_item_ids=item_ids(),
        )
        self.assertIsInstance(compiled.predicate, ConstraintPredicateV1)
        self.assertEqual(compiled.predicate.constraint_ref.constraint_id, CONSTRAINT_ID)
        self.assertEqual(compiled.protected, True)
        self.assertEqual(compiled.source_kind, CriterionSourceKindV1.USER)

    def test_derive_source_prefers_goal_then_plan(self) -> None:
        goal_draft = valid_draft().success_criteria[0]
        self.assertEqual(
            derive_criterion_source(goal_draft),
            (CriterionSourceKindV1.GOAL, ("goalcriterion1",)),
        )
        plan_draft = goal_draft.model_copy(update={"goal_criterion_local_id": None})
        self.assertEqual(
            derive_criterion_source(plan_draft),
            (CriterionSourceKindV1.PLAN, ("criterion1",)),
        )

    def test_criterion_id_and_digest_are_deterministic(self) -> None:
        draft = valid_draft().success_criteria[0]
        first = compile_criterion(
            task_id=TASK_ID,
            plan_version=1,
            draft=draft,
            source_kind=CriterionSourceKindV1.GOAL,
            source_refs=("goalcriterion1",),
            plan_item_ids=item_ids(),
        )
        second = compile_criterion(
            task_id=TASK_ID,
            plan_version=1,
            draft=draft,
            source_kind=CriterionSourceKindV1.GOAL,
            source_refs=("goalcriterion1",),
            plan_item_ids=item_ids(),
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first.digest), 64)

    def test_compiled_criterion_rejects_inconsistent_derivation(self) -> None:
        base = compile_criterion(
            task_id=TASK_ID,
            plan_version=1,
            draft=valid_draft().success_criteria[0],
            source_kind=CriterionSourceKindV1.GOAL,
            source_refs=("goalcriterion1",),
            plan_item_ids=item_ids(),
        )
        with self.assertRaises(ValidationError):
            CompiledCriterionV1.model_validate(
                {**base.model_dump(mode="json"), "requirement_level": "SUPPORTING"}
            )

    def test_unresolved_producer_item_raises(self) -> None:
        draft = valid_draft().success_criteria[0]
        with self.assertRaises(ValueError):
            compile_criterion(
                task_id=TASK_ID,
                plan_version=1,
                draft=draft,
                source_kind=CriterionSourceKindV1.GOAL,
                source_refs=("goalcriterion1",),
                plan_item_ids={},
            )


class PlanCompilerTests(unittest.TestCase):
    def test_compile_plan_resolves_ids_and_injects_stops(self) -> None:
        compiled = compile_plan(
            task_id=TASK_ID,
            plan_version=1,
            predecessor_version=None,
            input_id=INPUT_ID,
            draft=valid_draft(),
            planning_view=planning_view(),
            deadline_at=future_deadline(),
        )
        self.assertEqual(compiled.contract_version, "task_plan_version_v1")
        self.assertEqual(compiled.plan_version, 1)
        self.assertIsNone(compiled.predecessor_version)
        self.assertEqual(len(compiled.items), 1)
        self.assertEqual(len(compiled.hypotheses), 1)
        self.assertEqual(len(compiled.compiled_criteria), 1)
        self.assertEqual(len(compiled.stop_conditions), 5)
        kinds = {stop.kind for stop in compiled.stop_conditions}
        self.assertEqual(kinds, set(StopConditionKindV1))
        self.assertEqual(compiled.initial_ready_item_ids, (item_ids()["item1"],))
        self.assertEqual(len(compiled.compiled_plan_digest), 64)

    def test_compile_plan_is_deterministic(self) -> None:
        first = compile_plan(
            task_id=TASK_ID,
            plan_version=1,
            predecessor_version=None,
            input_id=INPUT_ID,
            draft=valid_draft(),
            planning_view=planning_view(),
            deadline_at=future_deadline(),
        )
        second = compile_plan(
            task_id=TASK_ID,
            plan_version=1,
            predecessor_version=None,
            input_id=INPUT_ID,
            draft=valid_draft(),
            planning_view=planning_view(),
            deadline_at=future_deadline(),
        )
        self.assertEqual(first, second)

    def test_item_resolves_candidate_capability_and_dependency(self) -> None:
        draft = valid_draft().model_copy(
            update={
                "items": (
                    valid_draft().items[0].model_copy(
                        update={"depends_on_local_ids": ()}
                    ),
                    PlanItemDraftV1(
                        local_id="item2",
                        objective="summarize",
                        executor_requirement="SPECIALIST",
                        capability_requirements=(),
                        input_artifact_refs=(),
                        output_schema_ref="summary-v1@1",
                        depends_on_local_ids=("item1",),
                        iteration_policy=IterationPolicyV1(
                            mode="SINGLE_PASS", max_iterations=1, stop_criterion_ids=()
                        ),
                    ),
                ),
                "initial_ready_item_local_ids": ("item1",),
            }
        )
        compiled = compile_plan(
            task_id=TASK_ID,
            plan_version=1,
            predecessor_version=None,
            input_id=INPUT_ID,
            draft=draft,
            planning_view=planning_view(),
            deadline_at=future_deadline(),
        )
        item1, item2 = compiled.items
        self.assertEqual(item1.candidate_capability_refs[0].capability_id, "writing-v1")
        self.assertEqual(item1.initial_state, "READY")
        self.assertEqual(item2.initial_state, "OPEN")
        self.assertEqual(item2.depends_on_item_ids, (item1.plan_item_id,))

    def test_hypothesis_id_is_stable(self) -> None:
        self.assertEqual(
            str(HYPOTHESIS_NAMESPACE), "b44cd5b1-194f-5076-8afc-91ada0c221fa"
        )
        self.assertEqual(
            str(hypothesis_id(TASK_ID, 1, "h1")),
            "585309ce-c721-5e1b-b16f-3cd502ff8acf",
        )

    def test_first_plan_rejects_predecessor(self) -> None:
        with self.assertRaises(ValidationError):
            CompiledPlanV1.model_validate(
                {
                    "contract_version": "task_plan_version_v1",
                    "task_id": str(TASK_ID),
                    "plan_version": 1,
                    "predecessor_version": 0,
                    "input_id": str(INPUT_ID),
                    "goal_understanding_ref": artifact_ref().model_dump(mode="json"),
                    "hypotheses": (),
                    "items": (
                        {
                            "plan_item_id": str(item_ids()["item1"]),
                            "source_local_id": "item1",
                            "objective": "x",
                            "executor_requirement": "SPECIALIST",
                            "capability_requirements": (),
                            "candidate_capability_refs": (),
                            "input_artifact_refs": (),
                            "output_schema_ref": "s@1",
                            "depends_on_item_ids": (),
                            "iteration_policy": {
                                "mode": "SINGLE_PASS",
                                "max_iterations": 1,
                                "stop_criterion_ids": (),
                            },
                            "initial_state": "READY",
                            "definition_digest": ZERO_SHA,
                        },
                    ),
                    "compiled_criteria": (),
                    "stop_conditions": (),
                    "initial_ready_item_ids": (str(item_ids()["item1"]),),
                    "revision_reason": "NEW_TASK",
                    "compiled_plan_digest": ZERO_SHA,
                }
            )


class PlanValidatorTests(unittest.TestCase):
    def test_valid_draft_is_accepted(self) -> None:
        result = validate_plan(
            task_id=TASK_ID,
            plan_version=1,
            predecessor_version=None,
            input_id=INPUT_ID,
            draft=valid_draft(),
            goal_criterion_ids={"goalcriterion1"},
            planning_view=planning_view(),
            deadline_at=future_deadline(),
            now=now(),
        )
        self.assertEqual(result.outcome, "ACCEPT")
        self.assertEqual(result.reason_codes, ())
        self.assertEqual(len(result.compiled_plan_digest), 64)

    def test_unresolved_capability_is_unexecutable(self) -> None:
        empty_view = planning_view().model_copy(update={"descriptors": ()})
        result = validate_plan(
            task_id=TASK_ID,
            plan_version=1,
            predecessor_version=None,
            input_id=INPUT_ID,
            draft=valid_draft(),
            goal_criterion_ids={"goalcriterion1"},
            planning_view=empty_view,
            deadline_at=future_deadline(),
            now=now(),
        )
        self.assertEqual(result.outcome, "UNEXECUTABLE")
        self.assertIn(
            InitialPlanValidationReasonV1.CAPABILITY_REQUIREMENT_UNRESOLVED,
            result.reason_codes,
        )
        self.assertIsNone(result.compiled_plan_digest)

    def test_deadline_exceeded_is_unexecutable(self) -> None:
        result = validate_plan(
            task_id=TASK_ID,
            plan_version=1,
            predecessor_version=None,
            input_id=INPUT_ID,
            draft=valid_draft(),
            goal_criterion_ids={"goalcriterion1"},
            planning_view=planning_view(),
            deadline_at=now(),
            now=datetime(2026, 10, 3, 0, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(result.outcome, "UNEXECUTABLE")
        self.assertIn(InitialPlanValidationReasonV1.DEADLINE_EXCEEDED, result.reason_codes)

    def test_budget_insufficient_is_unexecutable(self) -> None:
        result = validate_plan(
            task_id=TASK_ID,
            plan_version=1,
            predecessor_version=None,
            input_id=INPUT_ID,
            draft=valid_draft(),
            goal_criterion_ids={"goalcriterion1"},
            planning_view=planning_view(),
            deadline_at=future_deadline(),
            now=now(),
            budget_sufficient=False,
        )
        self.assertEqual(result.outcome, "UNEXECUTABLE")
        self.assertIn(InitialPlanValidationReasonV1.BUDGET_INSUFFICIENT, result.reason_codes)

    def test_missing_core_deliverable_is_repairable(self) -> None:
        draft = valid_draft().model_copy(
            update={
                "success_criteria": (
                    valid_draft().success_criteria[0].model_copy(
                        update={
                            "target": CitationCriterionTargetV1(
                                kind="CITATION",
                                producer_item_local_id="item1",
                                min_citations=1,
                            )
                        }
                    ),
                )
            }
        )
        result = validate_plan(
            task_id=TASK_ID,
            plan_version=1,
            predecessor_version=None,
            input_id=INPUT_ID,
            draft=draft,
            goal_criterion_ids={"goalcriterion1"},
            planning_view=planning_view(),
            deadline_at=future_deadline(),
            now=now(),
        )
        self.assertEqual(result.outcome, "REPAIRABLE_INVALID")
        self.assertIn(InitialPlanValidationReasonV1.SCHEMA_INVALID, result.reason_codes)

    def test_goal_coverage_missing_is_repairable(self) -> None:
        result = validate_plan(
            task_id=TASK_ID,
            plan_version=1,
            predecessor_version=None,
            input_id=INPUT_ID,
            draft=valid_draft(),
            goal_criterion_ids={"goalcriterion1", "goalcriterion2"},
            planning_view=planning_view(),
            deadline_at=future_deadline(),
            now=now(),
        )
        self.assertEqual(result.outcome, "REPAIRABLE_INVALID")
        self.assertIn(
            InitialPlanValidationReasonV1.CRITERION_COVERAGE_MISSING, result.reason_codes
        )


if __name__ == "__main__":
    unittest.main()
