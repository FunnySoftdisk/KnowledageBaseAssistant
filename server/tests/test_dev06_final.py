"""DEV-06 Final 综合离线验收：快照冻结、候选契约与完成判定真值表。

不接真实模型/PG/Catalog；只证明 `FinalSynthesisSnapshotV1` 确定性冻结、`final_synthesis_v1`
候选严格 Schema 与零 LLM `CompletionGateV1`/`DeliverableUsabilityValidatorV1` 判定正确性。
"""

import unittest
from datetime import UTC, datetime, timedelta, timezone
from unittest import mock
from uuid import UUID

from pydantic import ValidationError

from knowledge_system.modules.tasking.domain import final_candidate as final_candidate_mod
from knowledge_system.modules.tasking.domain import final_synthesis as final_synthesis_mod
from knowledge_system.modules.tasking.domain.compiled_criterion import (
    ArtifactContractPredicateV1,
    CompiledCriterionV1,
    EvidenceSupportPredicateV1,
    StateFactPredicateV1,
)
from knowledge_system.modules.tasking.domain.core_types import (
    ConstraintRefV1,
    PolicySnapshotRefV1,
    SchemaRefV1,
    VersionedRefV1,
)
from knowledge_system.modules.tasking.domain.final_candidate import (
    FinalArtifactLinkBlockV1,
    FinalClaimDraftV1,
    FinalCodeBlockV1,
    FinalGapReportV1,
    FinalListBlockV1,
    FinalParagraphBlockV1,
    FinalSectionDraftV1,
    FinalSynthesisCandidateV1,
    FinalTableBlockV1,
    SemanticCriterionAssessmentV1,
    candidate_canonical_bytes,
    validate_candidate_size,
)
from knowledge_system.modules.tasking.domain.final_gate import (
    ConstraintCheckV1,
    CriterionEvaluationV1,
    decide_completion,
    deliverable_usability,
    freeze_criterion_evaluation,
    semantic_assessment_status,
)
from knowledge_system.modules.tasking.domain.final_gate import (
    CriterionEvaluationStatusV1 as Status,
)
from knowledge_system.modules.tasking.domain.final_synthesis import (
    FinalProtectedBudgetV1,
    FinalSynthesisSnapshotV1,
    final_synthesis_snapshot_id,
    freeze_final_synthesis_snapshot,
    snapshot_canonical_bytes,
    snapshot_digest_for,
)
from knowledge_system.modules.tasking.domain.input_contracts import OutputContractV1
from knowledge_system.modules.tasking.domain.planning_fragments import CriterionSourceKindV1
from knowledge_system.modules.tasking.domain.runtime_decision import FinalOutcomeKindV1

ZERO_SHA = "0" * 64
TASK_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
DECISION_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
INPUT_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
PLAN_ITEM_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
SNAPSHOT_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
POLICY_ID = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
CORE_CRITERION_ID = UUID("00000000-0000-4000-8000-000000000001")
AUX_CRITERION_ID = UUID("00000000-0000-4000-8000-000000000002")
CORE2_CRITERION_ID = UUID("00000000-0000-4000-8000-000000000003")
CONSTRAINT_ID = UUID("00000000-0000-4000-8000-000000000004")
BUILDER_PROFILE = "final-synthesis-context-builder-v1"


def compiled_criterion(role, predicate_kind, criterion_id):
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
    elif predicate_kind == "STATE_FACT":
        predicate = StateFactPredicateV1(
            kind="STATE_FACT",
            subject_plan_item_id=PLAN_ITEM_ID,
            expected_state="COMPLETED",
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


def versioned_ref(ref_id):
    return VersionedRefV1(ref_id=ref_id, version=1, integrity_digest=ZERO_SHA)


def constraint_ref(constraint_id):
    return ConstraintRefV1(constraint_id=constraint_id, input_id=INPUT_ID, parser_version="1")


def snapshot_kwargs(**overrides):
    kwargs = {
        "contract_version": "final_synthesis_snapshot_v1",
        "task_id": TASK_ID,
        "runtime_decision_id": DECISION_ID,
        "input_id": INPUT_ID,
        "plan_version": 1,
        "work_version": 1,
        "execution_generation": 0,
        "cancellation_generation": 0,
        "event_high_watermark": 7,
        "compiled_criteria": (
            compiled_criterion("CORE_DELIVERABLE", "ARTIFACT_CONTRACT", CORE_CRITERION_ID),
        ),
        "effective_constraint_refs": (),
        "output_contract": OutputContractV1(
            format="MARKDOWN",
            delivery="INLINE",
            language="zh-CN",
            schema_ref=None,
            template_or_skill_ref=None,
            citations="REQUIRED",
            max_output_bytes=1_048_576,
        ),
        "adopted_result_refs": (),
        "gap_summaries": (),
        "conflict_ids": (),
        "unresolved_subgoal_ids": (),
        "permanent_blocker_ids": (),
        "citation_candidate_catalog_ids": (),
        "result_artifact_candidate_catalog_ids": (),
        "policy_snapshot_ref": PolicySnapshotRefV1(
            policy_snapshot_id=POLICY_ID,
            policy_revision=1,
            policy_digest=ZERO_SHA,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        "source_visibility": (),
        "authorization_revoked": False,
        "degraded": False,
        "final_protected_budget": FinalProtectedBudgetV1(
            model_requests=2, input_tokens=32768, output_tokens=16384
        ),
        "deadline_at": datetime(2026, 1, 2, tzinfo=UTC),
        "builder_profile": BUILDER_PROFILE,
        "builder_schema_ref": SchemaRefV1(
            schema_id="final-synthesis-schema", schema_version="1", schema_digest=ZERO_SHA
        ),
        "snapshot_digest": ZERO_SHA,
    }
    kwargs.update(overrides)
    return kwargs


def snapshot(**overrides):
    return FinalSynthesisSnapshotV1(**snapshot_kwargs(**overrides))


def paragraph(claim_ids):
    return FinalParagraphBlockV1(block_kind="PARAGRAPH", claim_local_ids=claim_ids)


def section(section_id, blocks):
    return FinalSectionDraftV1(local_section_id=section_id, heading="Section", blocks=blocks)


def claim(claim_id):
    return FinalClaimDraftV1(
        local_claim_id=claim_id,
        claim_kind="SOURCE_FACT",
        text="A fact",
        support_catalog_ids=("cat-1",),
        criterion_ids=(),
        conflict_set_ids=(),
    )


def candidate_kwargs(**overrides):
    kwargs = {
        "contract_version": "final_synthesis_v1",
        "snapshot_id": SNAPSHOT_ID,
        "snapshot_digest": ZERO_SHA,
        "title": "Final answer",
        "claims": (claim("claim-1"),),
        "sections": (section("sec-1", (paragraph(("claim-1",)),)),),
        "semantic_criterion_assessments": (),
        "gap_reports": (),
        "selected_result_artifact_catalog_ids": (),
    }
    kwargs.update(overrides)
    return kwargs


def candidate(**overrides):
    return FinalSynthesisCandidateV1(**candidate_kwargs(**overrides))


def evaluation(criterion_id, status):
    return freeze_criterion_evaluation(
        CriterionEvaluationV1(criterion_id=criterion_id, status=status, evidence_refs=())
    )


class FinalSynthesisSnapshotTests(unittest.TestCase):
    def test_extra_field_forbidden(self):
        with self.assertRaises(ValidationError):
            FinalSynthesisSnapshotV1(**{**snapshot_kwargs(), "bogus": 1})

    def test_strict_coercion_rejected(self):
        with self.assertRaises(ValidationError):
            snapshot(plan_version="1")

    def test_naive_deadline_rejected(self):
        with self.assertRaises(ValidationError):
            snapshot(deadline_at=datetime(2026, 1, 2))

    def test_deadline_normalized_to_utc(self):
        value = datetime(2026, 1, 2, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(
            snapshot(deadline_at=value).deadline_at, datetime(2026, 1, 2, 4, tzinfo=UTC)
        )

    def test_digest_deterministic_and_non_placeholder(self):
        frozen = freeze_final_synthesis_snapshot(snapshot())
        again = freeze_final_synthesis_snapshot(snapshot())
        self.assertEqual(frozen.snapshot_digest, again.snapshot_digest)
        self.assertNotEqual(frozen.snapshot_digest, ZERO_SHA)

    def test_digest_differs_on_field_change(self):
        frozen_a = freeze_final_synthesis_snapshot(snapshot())
        frozen_b = freeze_final_synthesis_snapshot(snapshot(event_high_watermark=8))
        self.assertNotEqual(frozen_a.snapshot_digest, frozen_b.snapshot_digest)

    def test_freeze_digest_matches_snapshot_digest_for(self):
        value = snapshot()
        frozen = freeze_final_synthesis_snapshot(value)
        self.assertEqual(frozen.snapshot_digest, snapshot_digest_for(value))

    def test_snapshot_id_deterministic(self):
        digest = freeze_final_synthesis_snapshot(snapshot()).snapshot_digest
        first = final_synthesis_snapshot_id(
            task_id=TASK_ID,
            runtime_decision_id=DECISION_ID,
            snapshot_digest=digest,
            builder_profile=BUILDER_PROFILE,
        )
        second = final_synthesis_snapshot_id(
            task_id=TASK_ID,
            runtime_decision_id=DECISION_ID,
            snapshot_digest=digest,
            builder_profile=BUILDER_PROFILE,
        )
        self.assertEqual(first, second)
        other = final_synthesis_snapshot_id(
            task_id=TASK_ID,
            runtime_decision_id=DECISION_ID,
            snapshot_digest=ZERO_SHA,
            builder_profile=BUILDER_PROFILE,
        )
        self.assertNotEqual(first, other)

    def test_too_large_rejected(self):
        with mock.patch.object(final_synthesis_mod, "FINAL_SYNTHESIS_SNAPSHOT_MAX_BYTES", 10):
            with self.assertRaises(ValueError) as ctx:
                freeze_final_synthesis_snapshot(snapshot())
        self.assertIn("FINAL_SYNTHESIS_SNAPSHOT_TOO_LARGE", str(ctx.exception))

    def test_canonical_bytes_positive(self):
        self.assertGreater(snapshot_canonical_bytes(snapshot()), 0)

    def test_effective_constraint_refs(self):
        value = snapshot(effective_constraint_refs=(constraint_ref(CONSTRAINT_ID),))
        self.assertEqual(len(value.effective_constraint_refs), 1)


class FinalCandidateContractTests(unittest.TestCase):
    def test_extra_field_forbidden(self):
        with self.assertRaises(ValidationError):
            FinalSynthesisCandidateV1(**{**candidate_kwargs(), "bogus": 1})

    def test_section_or_gap_required(self):
        with self.assertRaises(ValidationError) as ctx:
            candidate(sections=(), gap_reports=())
        self.assertIn("CANDIDATE_SECTION_OR_GAP_REQUIRED", str(ctx.exception))

    def test_paragraph_claim_count_bounds(self):
        with self.assertRaises(ValidationError):
            paragraph(())
        with self.assertRaises(ValidationError):
            paragraph(tuple(f"c{i}" for i in range(9)))

    def test_list_item_claim_count_bounds(self):
        with self.assertRaises(ValidationError):
            FinalListBlockV1(block_kind="BULLET_LIST", ordered=False, items=(("c1",), ()))
        with self.assertRaises(ValidationError):
            FinalListBlockV1(
                block_kind="BULLET_LIST", ordered=False, items=(("c1", "c2", "c3", "c4", "c5"),)
            )

    def test_table_column_count_mismatch(self):
        with self.assertRaises(ValidationError):
            FinalTableBlockV1(
                block_kind="TABLE",
                column_count=2,
                rows=((("c1",), ("c2",)), (("c3",),)),
            )

    def test_table_cell_claim_count_bounds(self):
        with self.assertRaises(ValidationError):
            FinalTableBlockV1(
                block_kind="TABLE",
                column_count=1,
                rows=((("c1", "c2", "c3", "c4", "c5"),),),
            )

    def test_code_and_artifact_link_blocks(self):
        code = FinalCodeBlockV1(
            block_kind="CODE", language="python", code="print(1)", support_catalog_ids=()
        )
        link = FinalArtifactLinkBlockV1(
            block_kind="ARTIFACT_LINK", catalog_id="cat-1", display_name="Report"
        )
        self.assertEqual(code.block_kind, "CODE")
        self.assertEqual(link.display_name, "Report")

    def test_total_block_limit(self):
        blocks = tuple(paragraph(("c1",)) for _ in range(13))
        sections = tuple(section(f"sec-{i}", blocks) for i in range(5))
        with self.assertRaises(ValidationError) as ctx:
            candidate(sections=sections)
        self.assertIn("CANDIDATE_TOTAL_BLOCK_LIMIT", str(ctx.exception))

    def test_duplicate_claim_id(self):
        with self.assertRaises(ValidationError):
            candidate(claims=(claim("c1"), claim("c1")))

    def test_duplicate_section_id(self):
        with self.assertRaises(ValidationError):
            candidate(
                sections=(
                    section("sec-1", (paragraph(("c1",)),)),
                    section("sec-1", (paragraph(("c1",)),)),
                )
            )

    def test_duplicate_gap_id(self):
        gap_a = FinalGapReportV1(
            local_gap_id="gap-1",
            gap_kind="EVIDENCE_MISSING",
            description="missing",
            source_gap_ids=(),
            affected_criterion_ids=(),
            next_steps=(),
        )
        with self.assertRaises(ValidationError):
            candidate(sections=(), gap_reports=(gap_a, gap_a))

    def test_duplicate_criterion_assessment(self):
        assessment_a = SemanticCriterionAssessmentV1(
            criterion_id=CORE_CRITERION_ID,
            assessment="SUPPORTED",
            supporting_catalog_ids=(),
            claim_local_ids=(),
            note="ok",
        )
        with self.assertRaises(ValidationError):
            candidate(semantic_criterion_assessments=(assessment_a, assessment_a))

    def test_canonical_bytes_deterministic(self):
        first = candidate_canonical_bytes(candidate())
        second = candidate_canonical_bytes(candidate())
        self.assertEqual(first, second)
        self.assertGreater(first, 0)

    def test_size_limit_validator(self):
        with mock.patch.object(final_candidate_mod, "CANDIDATE_MAX_BYTES", 10):
            with self.assertRaises(ValueError) as ctx:
                validate_candidate_size(candidate())
        self.assertIn("CANDIDATE_SIZE_LIMIT_EXCEEDED", str(ctx.exception))


class CompletionGateTests(unittest.TestCase):
    def _core(self):
        return compiled_criterion("CORE_DELIVERABLE", "ARTIFACT_CONTRACT", CORE_CRITERION_ID)

    def _aux(self):
        return compiled_criterion("REQUIRED_AUXILIARY", "EVIDENCE_SUPPORT", AUX_CRITERION_ID)

    def test_semantic_assessment_mapping(self):
        self.assertEqual(semantic_assessment_status("SUPPORTED"), Status.SATISFIED)
        self.assertEqual(semantic_assessment_status("PARTIALLY_SUPPORTED"), Status.PARTIAL)
        self.assertEqual(semantic_assessment_status("UNSUPPORTED"), Status.UNSATISFIED)
        self.assertEqual(semantic_assessment_status("UNKNOWN"), Status.UNKNOWN)

    def test_complete(self):
        decision = decide_completion(
            compiled_criteria=(self._core(), self._aux()),
            evaluations=(
                evaluation(CORE_CRITERION_ID, Status.SATISFIED),
                evaluation(AUX_CRITERION_ID, Status.SATISFIED),
            ),
            constraint_checks=(),
            output_contract_complete=True,
            substantive_claims_supported=True,
            has_critical_degradation=False,
        )
        self.assertEqual(decision.decision, FinalOutcomeKindV1.COMPLETE)
        self.assertEqual(decision.unmet_required_criterion_ids, ())
        self.assertEqual(decision.unmet_constraint_ids, ())

    def test_partial_when_auxiliary_partial(self):
        decision = decide_completion(
            compiled_criteria=(self._core(), self._aux()),
            evaluations=(
                evaluation(CORE_CRITERION_ID, Status.SATISFIED),
                evaluation(AUX_CRITERION_ID, Status.PARTIAL),
            ),
            constraint_checks=(),
            output_contract_complete=True,
            substantive_claims_supported=True,
            has_critical_degradation=False,
        )
        self.assertEqual(decision.decision, FinalOutcomeKindV1.PARTIAL)
        self.assertEqual(decision.unmet_required_criterion_ids, (AUX_CRITERION_ID,))

    def test_insufficient_evidence_when_core_unsatisfied(self):
        decision = decide_completion(
            compiled_criteria=(self._core(), self._aux()),
            evaluations=(
                evaluation(CORE_CRITERION_ID, Status.UNSATISFIED),
                evaluation(AUX_CRITERION_ID, Status.SATISFIED),
            ),
            constraint_checks=(),
            output_contract_complete=True,
            substantive_claims_supported=True,
            has_critical_degradation=False,
        )
        self.assertEqual(decision.decision, FinalOutcomeKindV1.INSUFFICIENT_EVIDENCE)

    def test_insufficient_evidence_when_claims_unsupported(self):
        decision = decide_completion(
            compiled_criteria=(self._core(), self._aux()),
            evaluations=(
                evaluation(CORE_CRITERION_ID, Status.SATISFIED),
                evaluation(AUX_CRITERION_ID, Status.SATISFIED),
            ),
            constraint_checks=(),
            output_contract_complete=True,
            substantive_claims_supported=False,
            has_critical_degradation=False,
        )
        self.assertEqual(decision.decision, FinalOutcomeKindV1.INSUFFICIENT_EVIDENCE)

    def test_not_applicable_is_skipped(self):
        core2 = compiled_criterion("CORE_DELIVERABLE", "ARTIFACT_CONTRACT", CORE2_CRITERION_ID)
        decision = decide_completion(
            compiled_criteria=(self._core(), core2),
            evaluations=(
                evaluation(CORE_CRITERION_ID, Status.NOT_APPLICABLE),
                evaluation(CORE2_CRITERION_ID, Status.SATISFIED),
            ),
            constraint_checks=(),
            output_contract_complete=True,
            substantive_claims_supported=True,
            has_critical_degradation=False,
        )
        self.assertEqual(decision.decision, FinalOutcomeKindV1.COMPLETE)

    def test_constraint_unsatisfied_blocks_complete(self):
        decision = decide_completion(
            compiled_criteria=(self._core(), self._aux()),
            evaluations=(
                evaluation(CORE_CRITERION_ID, Status.SATISFIED),
                evaluation(AUX_CRITERION_ID, Status.SATISFIED),
            ),
            constraint_checks=(
                ConstraintCheckV1(constraint_id=CONSTRAINT_ID, status="UNSATISFIED"),
            ),
            output_contract_complete=True,
            substantive_claims_supported=True,
            has_critical_degradation=False,
        )
        self.assertEqual(decision.decision, FinalOutcomeKindV1.PARTIAL)
        self.assertEqual(decision.unmet_constraint_ids, (CONSTRAINT_ID,))

    def test_degradation_blocks_complete(self):
        decision = decide_completion(
            compiled_criteria=(self._core(), self._aux()),
            evaluations=(
                evaluation(CORE_CRITERION_ID, Status.SATISFIED),
                evaluation(AUX_CRITERION_ID, Status.SATISFIED),
            ),
            constraint_checks=(),
            output_contract_complete=True,
            substantive_claims_supported=True,
            has_critical_degradation=True,
        )
        self.assertEqual(decision.decision, FinalOutcomeKindV1.PARTIAL)

    def test_output_contract_incomplete_blocks_complete(self):
        decision = decide_completion(
            compiled_criteria=(self._core(), self._aux()),
            evaluations=(
                evaluation(CORE_CRITERION_ID, Status.SATISFIED),
                evaluation(AUX_CRITERION_ID, Status.SATISFIED),
            ),
            constraint_checks=(),
            output_contract_complete=False,
            substantive_claims_supported=True,
            has_critical_degradation=False,
        )
        self.assertEqual(decision.decision, FinalOutcomeKindV1.PARTIAL)

    def test_coverage_mismatch_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            decide_completion(
                compiled_criteria=(self._core(), self._aux()),
                evaluations=(evaluation(CORE_CRITERION_ID, Status.SATISFIED),),
                constraint_checks=(),
                output_contract_complete=True,
                substantive_claims_supported=True,
                has_critical_degradation=False,
            )
        self.assertIn("COMPLETION_EVALUATION_COVERAGE_MISMATCH", str(ctx.exception))

    def test_duplicate_evaluation_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            decide_completion(
                compiled_criteria=(self._core(),),
                evaluations=(
                    evaluation(CORE_CRITERION_ID, Status.SATISFIED),
                    evaluation(CORE_CRITERION_ID, Status.SATISFIED),
                ),
                constraint_checks=(),
                output_contract_complete=True,
                substantive_claims_supported=True,
                has_critical_degradation=False,
            )
        self.assertIn("COMPLETION_DUPLICATE_EVALUATION", str(ctx.exception))

    def test_no_applicable_core_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            decide_completion(
                compiled_criteria=(self._core(),),
                evaluations=(evaluation(CORE_CRITERION_ID, Status.NOT_APPLICABLE),),
                constraint_checks=(),
                output_contract_complete=True,
                substantive_claims_supported=True,
                has_critical_degradation=False,
            )
        self.assertIn("COMPLETION_NO_APPLICABLE_CORE", str(ctx.exception))

    def test_decision_digest_deterministic(self):
        args = {
            "compiled_criteria": (self._core(), self._aux()),
            "evaluations": (
                evaluation(CORE_CRITERION_ID, Status.SATISFIED),
                evaluation(AUX_CRITERION_ID, Status.SATISFIED),
            ),
            "constraint_checks": (),
            "output_contract_complete": True,
            "substantive_claims_supported": True,
            "has_critical_degradation": False,
        }
        first = decide_completion(**args)
        second = decide_completion(**args)
        self.assertEqual(first.decision_digest, second.decision_digest)
        self.assertNotEqual(first.decision_digest, ZERO_SHA)


class CriterionEvaluationTests(unittest.TestCase):
    def test_evaluation_digest_deterministic(self):
        first = evaluation(CORE_CRITERION_ID, Status.SATISFIED)
        second = evaluation(CORE_CRITERION_ID, Status.SATISFIED)
        self.assertEqual(first.evaluation_digest, second.evaluation_digest)
        self.assertNotEqual(first.evaluation_digest, ZERO_SHA)

    def test_evaluation_digest_reflects_status(self):
        satisfied = evaluation(CORE_CRITERION_ID, Status.SATISFIED)
        partial = evaluation(CORE_CRITERION_ID, Status.PARTIAL)
        self.assertNotEqual(satisfied.evaluation_digest, partial.evaluation_digest)

    def test_evidence_refs_limit(self):
        with self.assertRaises(ValidationError):
            CriterionEvaluationV1(
                criterion_id=CORE_CRITERION_ID,
                status=Status.SATISFIED,
                evidence_refs=tuple(versioned_ref(POLICY_ID) for _ in range(17)),
            )


class DeliverableUsabilityTests(unittest.TestCase):
    def test_complete_maps_usable_complete(self):
        self.assertEqual(
            deliverable_usability(decision=FinalOutcomeKindV1.COMPLETE, candidate_has_content=True),
            "USABLE_COMPLETE",
        )

    def test_partial_maps_usable_subset(self):
        self.assertEqual(
            deliverable_usability(decision=FinalOutcomeKindV1.PARTIAL, candidate_has_content=True),
            "USABLE_SUBSET",
        )

    def test_insufficient_maps_gap_report_only(self):
        self.assertEqual(
            deliverable_usability(
                decision=FinalOutcomeKindV1.INSUFFICIENT_EVIDENCE, candidate_has_content=True
            ),
            "GAP_REPORT_ONLY",
        )

    def test_no_decision_maps_invalid_candidate(self):
        self.assertEqual(
            deliverable_usability(decision=None, candidate_has_content=True),
            "INVALID_CANDIDATE",
        )

    def test_no_content_maps_invalid_candidate(self):
        self.assertEqual(
            deliverable_usability(
                decision=FinalOutcomeKindV1.COMPLETE, candidate_has_content=False
            ),
            "INVALID_CANDIDATE",
        )


if __name__ == "__main__":
    unittest.main()
