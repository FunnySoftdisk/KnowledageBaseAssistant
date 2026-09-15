"""已批准首轮Plan字段和关系；不证明Compiler/PG激活完成。"""

import unittest
from uuid import UUID, uuid4

from knowledge_system.modules.tasking.domain.core_types import ArtifactRefV1
from knowledge_system.modules.tasking.domain.planning_fragments import (
    PLAN_ITEM_NAMESPACE,
    ArtifactCriterionTargetV1,
    CapabilityRequirementV1,
    CriterionDraftV1,
    HypothesisDraftV1,
    InitialPlanDraftV1,
    InitialPlanValidationReasonV1,
    IterationPolicyV1,
    PlanItemDraftV1,
    plan_item_id,
    validate_plan_relations,
)
from pydantic import ValidationError

ZERO_SHA = "0" * 64


def artifact_ref() -> ArtifactRefV1:
    return ArtifactRefV1(
        artifact_id=uuid4(),
        artifact_type="GOAL_UNDERSTANDING",
        sha256=ZERO_SHA,
        schema_id="goal-understanding",
        schema_version="1",
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


class PlanningFragmentTests(unittest.TestCase):
    def test_iteration_relations(self) -> None:
        IterationPolicyV1(mode="SINGLE_PASS", max_iterations=1, stop_criterion_ids=())
        IterationPolicyV1(mode="BOUNDED_REPEAT", max_iterations=3, stop_criterion_ids=("c1",))
        invalid = (
            {"mode": "SINGLE_PASS", "max_iterations": 2, "stop_criterion_ids": ()},
            {"mode": "SINGLE_PASS", "max_iterations": 1, "stop_criterion_ids": ("c1",)},
            {"mode": "BOUNDED_REPEAT", "max_iterations": 1, "stop_criterion_ids": ("c1",)},
            {"mode": "BOUNDED_REPEAT", "max_iterations": 2, "stop_criterion_ids": ()},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                IterationPolicyV1.model_validate(value)

    def test_criterion_requires_typed_target(self) -> None:
        self.assertEqual(valid_draft().success_criteria[0].target.kind, "ARTIFACT")
        with self.assertRaises(ValidationError):
            CriterionDraftV1.model_validate(
                {
                    "local_id": "c1",
                    "statement": "bad",
                    "goal_criterion_local_id": None,
                    "evidence_kind": "ARTIFACT",
                    "target": {
                        "kind": "ARTIFACT",
                        "producer_item_local_id": "item1",
                        "output_schema_key": "schema@1",
                    },
                }
            )

    def test_relation_validator_accepts_valid_and_rejects_cycle(self) -> None:
        draft = valid_draft()
        self.assertEqual(validate_plan_relations(draft, {"goalcriterion1"}), ())
        bad = draft.model_copy(
            update={
                "items": (draft.items[0].model_copy(update={"depends_on_local_ids": ("item1",)}),)
            }
        )
        reasons = {issue.reason for issue in validate_plan_relations(bad, {"goalcriterion1"})}
        self.assertIn(InitialPlanValidationReasonV1.DEPENDENCY_MISSING, reasons)
        self.assertIn(InitialPlanValidationReasonV1.DEPENDENCY_CYCLE, reasons)
        self.assertIn(InitialPlanValidationReasonV1.INITIAL_READY_INVALID, reasons)

    def test_plan_item_uuid_profile_is_stable_and_checked(self) -> None:
        task_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        expected = UUID("f3f3a649-3e2d-5105-b985-7a31a1225ef7")
        self.assertEqual(str(PLAN_ITEM_NAMESPACE), "cf4b70b2-400e-5cf6-908f-b96949b4ceaf")
        self.assertEqual(plan_item_id(task_id, 1, "item1"), expected)
        for version, local_id in ((0, "item1"), (1, "UPPER"), (1, "../item")):
            with self.subTest(version=version, local_id=local_id), self.assertRaises(ValueError):
                plan_item_id(task_id, version, local_id)


if __name__ == "__main__":
    unittest.main()
