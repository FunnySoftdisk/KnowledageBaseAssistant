"""DEV-06 90 例 Gold 契约硬门禁：`final_synthesis_contract_gold_v1` 离线验收。

不接真实模型/PG，只驱动零 LLM 确定性完成判定（`decide_completion`），验证 90 例金标语料
（COMPLETE/PARTIAL/INSUFFICIENT_EVIDENCE 各 30 例）结构、三态分布、覆盖维度（中英文/引用/
Artifact/冲突/约束/拒批/禁网/能力/预算/注入与越权）与逐例通过；未核验 Claim（substantive
unsupported）与降级不得产出 COMPLETE。签名盲测分片、Replay/故障注入与 Canary 属真实运行，
不在此离线切片。
"""

from __future__ import annotations

import unittest
from uuid import UUID, uuid5

from knowledge_system.modules.tasking.domain.compiled_criterion import (
    ArtifactContractPredicateV1,
    CompiledCriterionV1,
    EvidenceSupportPredicateV1,
)
from knowledge_system.modules.tasking.domain.final_gate import (
    ConstraintCheckV1,
    CriterionEvaluationV1,
    freeze_criterion_evaluation,
)
from knowledge_system.modules.tasking.domain.final_gate import (
    CriterionEvaluationStatusV1 as Status,
)
from knowledge_system.modules.tasking.domain.final_gold_gate import (
    FINAL_GOLD_CASE_NAMESPACE,
    FINAL_SYNTHESIS_CONTRACT_GOLD_CASES,
    FINAL_SYNTHESIS_CONTRACT_GOLD_PER_OUTCOME,
    FinalGoldCaseCategoryV1,
    FinalGoldCaseV1,
    evaluate_final_gold_case,
    freeze_final_gold_case,
    gold_case_id,
    run_final_gold_gate,
)
from knowledge_system.modules.tasking.domain.planning_fragments import CriterionSourceKindV1
from knowledge_system.modules.tasking.domain.runtime_decision import FinalOutcomeKindV1

ZERO_SHA = "0" * 64
PLAN_ITEM_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
CATEGORIES = tuple(FinalGoldCaseCategoryV1)


def _criterion_id(outcome: FinalOutcomeKindV1, variant: int, index: int) -> UUID:
    return uuid5(FINAL_GOLD_CASE_NAMESPACE, f"{outcome}:{variant}:criterion:{index}")


def _constraint_id(outcome: FinalOutcomeKindV1, variant: int, index: int) -> UUID:
    return uuid5(FINAL_GOLD_CASE_NAMESPACE, f"{outcome}:{variant}:constraint:{index}")


def _criterion(role: str, predicate_kind: str, criterion_id: UUID) -> CompiledCriterionV1:
    if predicate_kind == "ARTIFACT_CONTRACT":
        predicate = ArtifactContractPredicateV1(
            kind="ARTIFACT_CONTRACT",
            producer_plan_item_id=PLAN_ITEM_ID,
            output_schema_key="report",
        )
    elif predicate_kind == "EVIDENCE_SUPPORT":
        predicate = EvidenceSupportPredicateV1(
            kind="EVIDENCE_SUPPORT",
            producer_plan_item_id=PLAN_ITEM_ID,
            min_citations=1,
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


def _evaluation(criterion_id: UUID, status: Status) -> CriterionEvaluationV1:
    return freeze_criterion_evaluation(
        CriterionEvaluationV1(criterion_id=criterion_id, status=status, evidence_refs=())
    )


def _constraint(constraint_id: UUID, status: str) -> ConstraintCheckV1:
    return ConstraintCheckV1(constraint_id=constraint_id, status=status)


def _case(outcome: FinalOutcomeKindV1, variant: int) -> FinalGoldCaseV1:
    num_core = 1 + (variant % 2)
    num_aux = variant % 2
    num_supporting = (variant // 2) % 2
    break_dim = variant % 4
    subtype = variant % 2

    if outcome is FinalOutcomeKindV1.PARTIAL and break_dim == 0:
        num_aux = 1  # 需要一个 aux 来制造未满足的必需项

    criteria: list[CompiledCriterionV1] = []
    evaluations: list[CriterionEvaluationV1] = []

    core_ids = [_criterion_id(outcome, variant, i) for i in range(num_core)]
    aux_ids = [_criterion_id(outcome, variant, 100 + i) for i in range(num_aux)]
    supporting_ids = [_criterion_id(outcome, variant, 200 + i) for i in range(num_supporting)]

    for i, criterion_id in enumerate(core_ids):
        criteria.append(_criterion("CORE_DELIVERABLE", "ARTIFACT_CONTRACT", criterion_id))
        if outcome is FinalOutcomeKindV1.COMPLETE:
            status = Status.SATISFIED
        elif outcome is FinalOutcomeKindV1.PARTIAL:
            status = Status.SATISFIED if i == 0 else Status.PARTIAL
        else:  # INSUFFICIENT_EVIDENCE
            status = Status.SATISFIED if subtype == 0 else Status.UNSATISFIED
        evaluations.append(_evaluation(criterion_id, status))

    for criterion_id in aux_ids:
        criteria.append(_criterion("REQUIRED_AUXILIARY", "ARTIFACT_CONTRACT", criterion_id))
        if outcome is FinalOutcomeKindV1.COMPLETE:
            status = Status.SATISFIED
        elif outcome is FinalOutcomeKindV1.PARTIAL and break_dim == 0:
            status = Status.UNSATISFIED
        elif outcome is FinalOutcomeKindV1.PARTIAL:
            status = Status.SATISFIED
        else:
            status = Status.UNSATISFIED
        evaluations.append(_evaluation(criterion_id, status))

    for criterion_id in supporting_ids:
        criteria.append(_criterion("SUPPORTING", "EVIDENCE_SUPPORT", criterion_id))
        status = Status.NOT_APPLICABLE if variant % 4 == 0 else Status.SATISFIED
        evaluations.append(_evaluation(criterion_id, status))

    constraints: list[ConstraintCheckV1] = []
    num_constraints = variant % 3
    for i in range(num_constraints):
        constraint_id = _constraint_id(outcome, variant, i)
        if outcome is FinalOutcomeKindV1.PARTIAL and break_dim == 1 and i == 0:
            constraints.append(_constraint(constraint_id, "UNSATISFIED"))
        else:
            constraints.append(_constraint(constraint_id, "SATISFIED"))

    output_contract_complete = not (outcome is FinalOutcomeKindV1.PARTIAL and break_dim == 2)
    substantive_claims_supported = not (
        outcome is FinalOutcomeKindV1.INSUFFICIENT_EVIDENCE and subtype == 0
    )
    has_critical_degradation = outcome is FinalOutcomeKindV1.PARTIAL and break_dim == 3

    return freeze_final_gold_case(
        FinalGoldCaseV1(
            contract_version="final_synthesis_contract_gold_v1",
            case_id=gold_case_id(outcome, variant),
            expected_outcome=outcome,
            language="ZH" if variant % 2 == 0 else "EN",
            category=CATEGORIES[variant % 4],
            repair_round="AFTER_REPAIR" if variant % 3 == 0 else "INITIAL",
            compiled_criteria=tuple(criteria),
            evaluations=tuple(evaluations),
            constraint_checks=tuple(constraints),
            output_contract_complete=output_contract_complete,
            substantive_claims_supported=substantive_claims_supported,
            has_critical_degradation=has_critical_degradation,
        )
    )


def build_gold_cases() -> tuple[FinalGoldCaseV1, ...]:
    cases: list[FinalGoldCaseV1] = []
    for outcome in FinalOutcomeKindV1:
        for variant in range(FINAL_SYNTHESIS_CONTRACT_GOLD_PER_OUTCOME):
            cases.append(_case(outcome, variant))
    return tuple(cases)


GOLD_CASES = build_gold_cases()


class GoldCorpusStructureTests(unittest.TestCase):
    def test_exactly_90_cases(self) -> None:
        self.assertEqual(len(GOLD_CASES), FINAL_SYNTHESIS_CONTRACT_GOLD_CASES)

    def test_30_cases_per_outcome(self) -> None:
        for outcome in FinalOutcomeKindV1:
            count = sum(1 for c in GOLD_CASES if c.expected_outcome is outcome)
            self.assertEqual(count, FINAL_SYNTHESIS_CONTRACT_GOLD_PER_OUTCOME)

    def test_case_ids_unique(self) -> None:
        ids = [case.case_id for case in GOLD_CASES]
        self.assertEqual(len(set(ids)), len(ids))

    def test_coverage_dimensions(self) -> None:
        languages = {case.language for case in GOLD_CASES}
        categories = {case.category for case in GOLD_CASES}
        rounds = {case.repair_round for case in GOLD_CASES}
        self.assertEqual(languages, {"ZH", "EN"})
        self.assertEqual(categories, set(CATEGORIES))
        self.assertEqual(rounds, {"INITIAL", "AFTER_REPAIR"})

    def test_case_digest_non_placeholder_and_deterministic(self) -> None:
        case = GOLD_CASES[0]
        self.assertNotEqual(case.case_digest, ZERO_SHA)
        rebuilt = freeze_final_gold_case(case.model_copy(update={"case_digest": ZERO_SHA}))
        self.assertEqual(rebuilt.case_digest, case.case_digest)


class GoldGateRunTests(unittest.TestCase):
    def test_all_90_cases_pass(self) -> None:
        report = run_final_gold_gate(GOLD_CASES)
        self.assertTrue(report.passed)
        self.assertEqual(report.passed_cases, FINAL_SYNTHESIS_CONTRACT_GOLD_CASES)
        self.assertEqual(report.complete_cases, FINAL_SYNTHESIS_CONTRACT_GOLD_PER_OUTCOME)
        self.assertEqual(report.partial_cases, FINAL_SYNTHESIS_CONTRACT_GOLD_PER_OUTCOME)
        self.assertEqual(
            report.insufficient_evidence_cases, FINAL_SYNTHESIS_CONTRACT_GOLD_PER_OUTCOME
        )
        self.assertNotEqual(report.report_digest, ZERO_SHA)

    def test_report_digest_deterministic(self) -> None:
        first = run_final_gold_gate(GOLD_CASES)
        second = run_final_gold_gate(GOLD_CASES)
        self.assertEqual(first.report_digest, second.report_digest)

    def test_evaluate_single_complete_case(self) -> None:
        case = next(c for c in GOLD_CASES if c.expected_outcome is FinalOutcomeKindV1.COMPLETE)
        verdict = evaluate_final_gold_case(case)
        self.assertTrue(verdict.passed)
        self.assertIs(verdict.produced_outcome, FinalOutcomeKindV1.COMPLETE)

    def test_unsupported_claims_never_complete(self) -> None:
        for case in GOLD_CASES:
            if not case.substantive_claims_supported:
                with self.subTest(case_id=case.case_id):
                    verdict = evaluate_final_gold_case(case)
                    self.assertIsNot(verdict.produced_outcome, FinalOutcomeKindV1.COMPLETE)

    def test_degradation_never_complete(self) -> None:
        for case in GOLD_CASES:
            if case.has_critical_degradation:
                with self.subTest(case_id=case.case_id):
                    verdict = evaluate_final_gold_case(case)
                    self.assertIsNot(verdict.produced_outcome, FinalOutcomeKindV1.COMPLETE)


class GoldGateValidationTests(unittest.TestCase):
    def test_wrong_case_count_rejected(self) -> None:
        with self.assertRaises(ValueError):
            run_final_gold_gate(GOLD_CASES[:89])

    def test_wrong_distribution_rejected(self) -> None:
        bad = tuple(
            case.model_copy(update={"expected_outcome": FinalOutcomeKindV1.COMPLETE})
            for case in GOLD_CASES[1:]
        ) + (GOLD_CASES[0],)
        with self.assertRaises(ValueError):
            run_final_gold_gate(bad)

    def test_duplicate_case_id_rejected(self) -> None:
        duplicate = GOLD_CASES[0]
        bad = (duplicate, duplicate) + GOLD_CASES[2:]
        with self.assertRaises(ValueError):
            run_final_gold_gate(bad)


if __name__ == "__main__":
    unittest.main()
