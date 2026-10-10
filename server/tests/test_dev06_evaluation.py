"""DEV-06 Final 离线验收：零 LLM `CriterionEvaluatorRegistryV1` 确定性谓词求值。

不接真实模型/PG/Catalog；只证明 `CriterionEvaluationStateV1` 状态投影契约、三类确定性谓词
（ARTIFACT_CONTRACT/STATE_FACT/CONSTRAINT）到 `CriterionEvaluationV1` 的映射，以及
`evaluate_all_criteria` 对确定性求值与语义评估的合并覆盖校验。
"""

import unittest
from uuid import UUID

from pydantic import ValidationError

from knowledge_system.modules.tasking.domain.compiled_criterion import (
    ArtifactContractPredicateV1,
    CompiledCriterionV1,
    ConstraintPredicateV1,
    EvidenceSupportPredicateV1,
    StateFactPredicateV1,
    UserFactPredicateV1,
)
from knowledge_system.modules.tasking.domain.core_types import ConstraintRefV1
from knowledge_system.modules.tasking.domain.final_candidate import SemanticCriterionAssessmentV1
from knowledge_system.modules.tasking.domain.final_evaluation import (
    AdoptedArtifactFactV1,
    CriterionEvaluationStateV1,
    evaluate_all_criteria,
    evaluate_criterion,
)
from knowledge_system.modules.tasking.domain.final_gate import (
    ConstraintCheckV1,
)
from knowledge_system.modules.tasking.domain.final_gate import (
    CriterionEvaluationStatusV1 as Status,
)
from knowledge_system.modules.tasking.domain.planning_fragments import CriterionSourceKindV1

ZERO_SHA = "0" * 64
TASK_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PLAN_ITEM_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
OTHER_ITEM_ID = UUID("dddddddd-dddd-4ddd-8ddd-ddddddddddee")
CONSTRAINT_ID = UUID("00000000-0000-4000-8000-000000000004")
CONSTRAINT2_ID = UUID("00000000-0000-4000-8000-000000000005")
CORE_CRITERION_ID = UUID("00000000-0000-4000-8000-000000000001")
SEM_CRITERION_ID = UUID("00000000-0000-4000-8000-000000000002")


def constraint_ref(constraint_id):
    return ConstraintRefV1(
        constraint_id=constraint_id,
        input_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        parser_version="1",
    )


def compiled_criterion(role, predicate_kind, criterion_id, **predicate_kwargs):
    if predicate_kind == "ARTIFACT_CONTRACT":
        predicate = ArtifactContractPredicateV1(
            kind="ARTIFACT_CONTRACT",
            producer_plan_item_id=predicate_kwargs.get("producer_plan_item_id", PLAN_ITEM_ID),
            output_schema_key=predicate_kwargs.get("output_schema_key", "report"),
        )
    elif predicate_kind == "EVIDENCE_SUPPORT":
        predicate = EvidenceSupportPredicateV1(
            kind="EVIDENCE_SUPPORT",
            producer_plan_item_id=predicate_kwargs.get("producer_plan_item_id", PLAN_ITEM_ID),
            min_citations=predicate_kwargs.get("min_citations", 1),
        )
    elif predicate_kind == "STATE_FACT":
        predicate = StateFactPredicateV1(
            kind="STATE_FACT",
            subject_plan_item_id=predicate_kwargs.get("subject_plan_item_id", PLAN_ITEM_ID),
            expected_state="COMPLETED",
        )
    elif predicate_kind == "CONSTRAINT":
        predicate = ConstraintPredicateV1(
            kind="CONSTRAINT",
            constraint_ref=predicate_kwargs.get("constraint_ref", constraint_ref(CONSTRAINT_ID)),
        )
    else:
        raise AssertionError(predicate_kind)
    mode = "EVIDENCE_GROUNDED_SEMANTIC" if predicate_kind == "EVIDENCE_SUPPORT" else "DETERMINISTIC"
    profile = (
        "evidence-grounded-semantic-v1"
        if mode == "EVIDENCE_GROUNDED_SEMANTIC"
        else "criterion-evaluator-registry-v1"
    )
    return CompiledCriterionV1(
        criterion_id=criterion_id,
        statement="criterion statement",
        source_kind=CriterionSourceKindV1.PLAN,
        source_refs=("criterion-source",),
        requirement_level="SUPPORTING" if role == "SUPPORTING" else "REQUIRED",
        completion_role=role,
        protected=(role == "CORE_DELIVERABLE"),
        evaluation_mode=mode,
        predicate=predicate,
        applicability=None,
        evaluator_profile=profile,
        digest=ZERO_SHA,
    )


def state(**overrides):
    defaults = dict(
        completed_item_ids=(),
        adopted_outputs=(),
        constraint_checks=(),
    )
    defaults.update(overrides)
    return CriterionEvaluationStateV1(**defaults)


def adopted(plan_item_id=PLAN_ITEM_ID, output_schema_key="report"):
    return AdoptedArtifactFactV1(
        producer_plan_item_id=plan_item_id, output_schema_key=output_schema_key
    )


def check(constraint_id=CONSTRAINT_ID, status="SATISFIED"):
    return ConstraintCheckV1(constraint_id=constraint_id, status=status)


def semantic(criterion_id, assessment):
    return SemanticCriterionAssessmentV1(
        criterion_id=criterion_id,
        assessment=assessment,
        supporting_catalog_ids=(),
        claim_local_ids=(),
        note="assessed",
    )


class CriterionEvaluationStateTests(unittest.TestCase):
    def test_valid_state_accepted(self):
        s = state(
            completed_item_ids=(PLAN_ITEM_ID,),
            adopted_outputs=(adopted(),),
            constraint_checks=(check(),),
        )
        self.assertEqual(s.completed_item_ids, (PLAN_ITEM_ID,))
        self.assertEqual(s.adopted_outputs, (adopted(),))

    def test_completed_item_duplicate_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            state(completed_item_ids=(PLAN_ITEM_ID, PLAN_ITEM_ID))
        self.assertIn("COMPLETED_ITEM_DUPLICATE", str(ctx.exception))

    def test_adopted_output_duplicate_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            state(adopted_outputs=(adopted(), adopted()))
        self.assertIn("ADOPTED_OUTPUT_DUPLICATE", str(ctx.exception))

    def test_constraint_check_duplicate_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            state(constraint_checks=(check(), check()))
        self.assertIn("CONSTRAINT_CHECK_DUPLICATE", str(ctx.exception))


class DeterministicEvaluationTests(unittest.TestCase):
    def test_artifact_contract_satisfied(self):
        criterion = compiled_criterion("CORE_DELIVERABLE", "ARTIFACT_CONTRACT", CORE_CRITERION_ID)
        result = evaluate_criterion(criterion, state(adopted_outputs=(adopted(),)))
        self.assertEqual(result.status, Status.SATISFIED)
        self.assertNotEqual(result.evaluation_digest, ZERO_SHA)

    def test_artifact_contract_unsatisfied(self):
        criterion = compiled_criterion("CORE_DELIVERABLE", "ARTIFACT_CONTRACT", CORE_CRITERION_ID)
        result = evaluate_criterion(criterion, state(adopted_outputs=()))
        self.assertEqual(result.status, Status.UNSATISFIED)

    def test_artifact_contract_wrong_schema_unsatisfied(self):
        criterion = compiled_criterion("CORE_DELIVERABLE", "ARTIFACT_CONTRACT", CORE_CRITERION_ID)
        result = evaluate_criterion(
            criterion, state(adopted_outputs=(adopted(output_schema_key="other"),))
        )
        self.assertEqual(result.status, Status.UNSATISFIED)

    def test_state_fact_satisfied(self):
        criterion = compiled_criterion("CORE_DELIVERABLE", "STATE_FACT", CORE_CRITERION_ID)
        result = evaluate_criterion(criterion, state(completed_item_ids=(PLAN_ITEM_ID,)))
        self.assertEqual(result.status, Status.SATISFIED)

    def test_state_fact_unsatisfied(self):
        criterion = compiled_criterion("CORE_DELIVERABLE", "STATE_FACT", CORE_CRITERION_ID)
        result = evaluate_criterion(criterion, state(completed_item_ids=(OTHER_ITEM_ID,)))
        self.assertEqual(result.status, Status.UNSATISFIED)

    def test_constraint_satisfied(self):
        criterion = compiled_criterion("CORE_DELIVERABLE", "CONSTRAINT", CORE_CRITERION_ID)
        result = evaluate_criterion(criterion, state(constraint_checks=(check(),)))
        self.assertEqual(result.status, Status.SATISFIED)

    def test_constraint_unsatisfied(self):
        criterion = compiled_criterion("CORE_DELIVERABLE", "CONSTRAINT", CORE_CRITERION_ID)
        result = evaluate_criterion(
            criterion, state(constraint_checks=(check(status="UNSATISFIED"),))
        )
        self.assertEqual(result.status, Status.UNSATISFIED)

    def test_constraint_unknown(self):
        criterion = compiled_criterion("CORE_DELIVERABLE", "CONSTRAINT", CORE_CRITERION_ID)
        result = evaluate_criterion(criterion, state(constraint_checks=(check(status="UNKNOWN"),)))
        self.assertEqual(result.status, Status.UNKNOWN)

    def test_constraint_check_missing_raises(self):
        criterion = compiled_criterion("CORE_DELIVERABLE", "CONSTRAINT", CORE_CRITERION_ID)
        with self.assertRaises(ValueError) as ctx:
            evaluate_criterion(criterion, state(constraint_checks=()))
        self.assertIn("CRITERION_CONSTRAINT_CHECK_MISSING", str(ctx.exception))

    def test_evidence_support_not_evaluable(self):
        criterion = compiled_criterion("CORE_DELIVERABLE", "EVIDENCE_SUPPORT", CORE_CRITERION_ID)
        with self.assertRaises(ValueError) as ctx:
            evaluate_criterion(criterion, state())
        self.assertIn("CRITERION_NOT_DETERMINISTICALLY_EVALUABLE", str(ctx.exception))

    def test_user_fact_not_evaluable(self):
        criterion = compiled_criterion("CORE_DELIVERABLE", "EVIDENCE_SUPPORT", CORE_CRITERION_ID)
        criterion = criterion.model_copy(
            update={"predicate": UserFactPredicateV1(kind="USER_FACT", statement="reserved")}
        )
        with self.assertRaises(ValueError) as ctx:
            evaluate_criterion(criterion, state())
        self.assertIn("CRITERION_NOT_DETERMINISTICALLY_EVALUABLE", str(ctx.exception))


class EvaluateAllCriteriaTests(unittest.TestCase):
    def test_mixed_deterministic_and_semantic(self):
        deterministic = compiled_criterion(
            "CORE_DELIVERABLE", "ARTIFACT_CONTRACT", CORE_CRITERION_ID
        )
        semantic_criterion = compiled_criterion(
            "CORE_DELIVERABLE", "EVIDENCE_SUPPORT", SEM_CRITERION_ID
        )
        evaluations = evaluate_all_criteria(
            compiled_criteria=(deterministic, semantic_criterion),
            state=state(adopted_outputs=(adopted(),)),
            semantic_assessments=(semantic(SEM_CRITERION_ID, "SUPPORTED"),),
        )
        by_id = {e.criterion_id: e for e in evaluations}
        self.assertEqual(by_id[CORE_CRITERION_ID].status, Status.SATISFIED)
        self.assertEqual(by_id[SEM_CRITERION_ID].status, Status.SATISFIED)
        self.assertEqual(len(evaluations), 2)

    def test_deterministic_only_needs_no_assessments(self):
        deterministic = compiled_criterion("CORE_DELIVERABLE", "STATE_FACT", CORE_CRITERION_ID)
        evaluations = evaluate_all_criteria(
            compiled_criteria=(deterministic,),
            state=state(completed_item_ids=(PLAN_ITEM_ID,)),
            semantic_assessments=(),
        )
        self.assertEqual(evaluations[0].status, Status.SATISFIED)

    def test_semantic_assessment_duplicate_raises(self):
        semantic_criterion = compiled_criterion(
            "CORE_DELIVERABLE", "EVIDENCE_SUPPORT", SEM_CRITERION_ID
        )
        with self.assertRaises(ValueError) as ctx:
            evaluate_all_criteria(
                compiled_criteria=(semantic_criterion,),
                state=state(),
                semantic_assessments=(
                    semantic(SEM_CRITERION_ID, "SUPPORTED"),
                    semantic(SEM_CRITERION_ID, "UNSUPPORTED"),
                ),
            )
        self.assertIn("SEMANTIC_ASSESSMENT_DUPLICATE", str(ctx.exception))

    def test_semantic_assessment_missing_raises(self):
        semantic_criterion = compiled_criterion(
            "CORE_DELIVERABLE", "EVIDENCE_SUPPORT", SEM_CRITERION_ID
        )
        with self.assertRaises(ValueError) as ctx:
            evaluate_all_criteria(
                compiled_criteria=(semantic_criterion,),
                state=state(),
                semantic_assessments=(),
            )
        self.assertIn("SEMANTIC_ASSESSMENT_COVERAGE_MISMATCH", str(ctx.exception))

    def test_semantic_assessment_for_deterministic_raises(self):
        deterministic = compiled_criterion(
            "CORE_DELIVERABLE", "ARTIFACT_CONTRACT", CORE_CRITERION_ID
        )
        with self.assertRaises(ValueError) as ctx:
            evaluate_all_criteria(
                compiled_criteria=(deterministic,),
                state=state(adopted_outputs=(adopted(),)),
                semantic_assessments=(semantic(CORE_CRITERION_ID, "SUPPORTED"),),
            )
        self.assertIn("SEMANTIC_ASSESSMENT_COVERAGE_MISMATCH", str(ctx.exception))

    def test_semantic_status_mapping_partial_and_unknown(self):
        semantic_criterion = compiled_criterion(
            "CORE_DELIVERABLE", "EVIDENCE_SUPPORT", SEM_CRITERION_ID
        )
        for assessment, expected in [
            ("SUPPORTED", Status.SATISFIED),
            ("PARTIALLY_SUPPORTED", Status.PARTIAL),
            ("UNSUPPORTED", Status.UNSATISFIED),
            ("UNKNOWN", Status.UNKNOWN),
        ]:
            evaluations = evaluate_all_criteria(
                compiled_criteria=(semantic_criterion,),
                state=state(),
                semantic_assessments=(semantic(SEM_CRITERION_ID, assessment),),
            )
            self.assertEqual(evaluations[0].status, expected)

    def test_evaluation_order_matches_compiled(self):
        first = compiled_criterion("CORE_DELIVERABLE", "STATE_FACT", CORE_CRITERION_ID)
        second = compiled_criterion("CORE_DELIVERABLE", "EVIDENCE_SUPPORT", SEM_CRITERION_ID)
        third = compiled_criterion(
            "CORE_DELIVERABLE",
            "CONSTRAINT",
            UUID("00000000-0000-4000-8000-000000000006"),
        )
        evaluations = evaluate_all_criteria(
            compiled_criteria=(first, second, third),
            state=state(
                completed_item_ids=(PLAN_ITEM_ID,),
                constraint_checks=(check(),),
            ),
            semantic_assessments=(semantic(SEM_CRITERION_ID, "SUPPORTED"),),
        )
        self.assertEqual(
            [e.criterion_id for e in evaluations],
            [first.criterion_id, second.criterion_id, third.criterion_id],
        )


if __name__ == "__main__":
    unittest.main()
