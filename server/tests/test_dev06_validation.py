"""DEV-06 Final 离线验收：零 LLM `FinalCandidateValidatorV1` 五态编排。

不接真实模型/Evidence/Catalog；只证明 `validate_final_candidate` 按 §5.33 固定顺序（Schema/128KiB、
Snapshot绑定、局部图引用、安全、Claim/Code支持、语义Criterion、Citation、Artifact/Output Contract）
首败即返，输出 `ACCEPT/REPAIRABLE_INVALID/SNAPSHOT_STALE/UNSAFE/UNRECOVERABLE_INVALID`，并携带
稳定原因码、有界修复路径与安全违规。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from knowledge_system.modules.tasking.domain.compiled_criterion import (
    EVALUATOR_PROFILE_EVIDENCE_GROUNDED,
    CompiledCriterionV1,
    EvidenceSupportPredicateV1,
)
from knowledge_system.modules.tasking.domain.core_types import (
    PolicySnapshotRefV1,
    SchemaRefV1,
    VersionedRefV1,
    canonical_json_sha256,
)
from knowledge_system.modules.tasking.domain.final_admission import FinalSupportCatalogEntryV1
from knowledge_system.modules.tasking.domain.final_candidate import (
    FinalArtifactLinkBlockV1,
    FinalClaimDraftV1,
    FinalCodeBlockV1,
    FinalParagraphBlockV1,
    FinalSectionDraftV1,
    FinalSynthesisCandidateV1,
    SemanticCriterionAssessmentV1,
)
from knowledge_system.modules.tasking.domain.final_citation import CitationSourceV1
from knowledge_system.modules.tasking.domain.final_evaluation import CriterionEvaluationStateV1
from knowledge_system.modules.tasking.domain.final_synthesis import (
    FinalProtectedBudgetV1,
    FinalSynthesisSnapshotV1,
    final_synthesis_snapshot_id,
    freeze_final_synthesis_snapshot,
)
from knowledge_system.modules.tasking.domain.final_validation import (
    FinalCandidateValidationV1,
    validate_final_candidate,
)
from knowledge_system.modules.tasking.domain.input_contracts import OutputContractV1
from knowledge_system.modules.tasking.domain.planning_fragments import CriterionSourceKindV1

ZERO_SHA = "0" * 64


def evid(i):
    return UUID(f"00000000-0000-4000-8000-{i:012d}")


def vref(ref_id):
    return VersionedRefV1(ref_id=ref_id, version=1, integrity_digest=ZERO_SHA)


def entry(
    catalog_id, kind, *, canonical_text=None, verdict=None, evidence_ref=None, artifact_kind=None
):
    return FinalSupportCatalogEntryV1(
        catalog_id=catalog_id,
        kind=kind,
        source_ref=vref(evid(900)),
        canonical_text=canonical_text,
        verdict=verdict,
        artifact_kind=artifact_kind,
        evidence_ref=evidence_ref,
    )


def _crit(criterion_id=None):
    return CompiledCriterionV1(
        criterion_id=criterion_id or uuid4(),
        statement="关键事实有据可查",
        source_kind=CriterionSourceKindV1.GOAL,
        source_refs=("gc1",),
        requirement_level="REQUIRED",
        completion_role="REQUIRED_AUXILIARY",
        protected=False,
        evaluation_mode="EVIDENCE_GROUNDED_SEMANTIC",
        predicate=EvidenceSupportPredicateV1(
            kind="EVIDENCE_SUPPORT", producer_plan_item_id=uuid4(), min_citations=1
        ),
        applicability=None,
        evaluator_profile=EVALUATOR_PROFILE_EVIDENCE_GROUNDED,
        digest=ZERO_SHA,
    )


def _contract(citations="OPTIONAL"):
    return OutputContractV1(
        format="MARKDOWN",
        delivery="INLINE",
        language="zh-CN",
        schema_ref=None,
        template_or_skill_ref=None,
        citations=citations,
        max_output_bytes=100_000,
    )


def snapshot(*, compiled_criteria=None, authorization_revoked=False, output_contract=None):
    return freeze_final_synthesis_snapshot(
        FinalSynthesisSnapshotV1(
            contract_version="final_synthesis_snapshot_v1",
            task_id=uuid4(),
            runtime_decision_id=uuid4(),
            input_id=uuid4(),
            plan_version=1,
            work_version=1,
            execution_generation=0,
            cancellation_generation=0,
            event_high_watermark=0,
            compiled_criteria=compiled_criteria if compiled_criteria is not None else (_crit(),),
            effective_constraint_refs=(),
            output_contract=output_contract or _contract(),
            adopted_result_refs=(),
            gap_summaries=(),
            conflict_ids=(),
            unresolved_subgoal_ids=(),
            permanent_blocker_ids=(),
            citation_candidate_catalog_ids=(),
            result_artifact_candidate_catalog_ids=(),
            policy_snapshot_ref=PolicySnapshotRefV1(
                policy_snapshot_id=uuid4(),
                policy_revision=1,
                policy_digest=ZERO_SHA,
                observed_at=datetime.now(UTC),
            ),
            source_visibility=(),
            authorization_revoked=authorization_revoked,
            degraded=False,
            final_protected_budget=FinalProtectedBudgetV1(
                model_requests=1, input_tokens=0, output_tokens=0
            ),
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
            builder_profile="final-synthesis-context-builder-v1",
            builder_schema_ref=SchemaRefV1(
                schema_id="final-synthesis", schema_version="v1", schema_digest=ZERO_SHA
            ),
            snapshot_digest=ZERO_SHA,
        )
    )


def snap_id(snap):
    return final_synthesis_snapshot_id(
        task_id=snap.task_id,
        runtime_decision_id=snap.runtime_decision_id,
        snapshot_digest=snap.snapshot_digest,
        builder_profile=snap.builder_profile,
    )


def semantic_id(snap):
    return next(c.criterion_id for c in snap.compiled_criteria)


def claim(local_claim_id, claim_kind, text, *, support=(), criteria=(), conflicts=()):
    return FinalClaimDraftV1(
        local_claim_id=local_claim_id,
        claim_kind=claim_kind,
        text=text,
        support_catalog_ids=support,
        criterion_ids=criteria,
        conflict_set_ids=conflicts,
    )


def paragraph(*claim_ids):
    return FinalParagraphBlockV1(block_kind="PARAGRAPH", claim_local_ids=claim_ids)


def section(local_section_id, *blocks):
    return FinalSectionDraftV1(local_section_id=local_section_id, heading="概述", blocks=blocks)


def candidate(
    snap,
    *,
    claims=None,
    sections=None,
    assessments=None,
    selected=(),
    snapshot_digest=None,
    title="报告标题",
):
    sem = semantic_id(snap)
    return FinalSynthesisCandidateV1(
        contract_version="final_synthesis_v1",
        snapshot_id=snap_id(snap),
        snapshot_digest=snapshot_digest if snapshot_digest is not None else snap.snapshot_digest,
        title=title,
        claims=claims
        if claims is not None
        else (claim("c1", "SOURCE_FACT", "事实A", support=("vc1",)),),
        sections=sections if sections is not None else (section("s1", paragraph("c1")),),
        semantic_criterion_assessments=assessments
        if assessments is not None
        else (
            SemanticCriterionAssessmentV1(
                criterion_id=sem,
                assessment="SUPPORTED",
                supporting_catalog_ids=("vc1",),
                claim_local_ids=("c1",),
                note="证据支持",
            ),
        ),
        gap_reports=(),
        selected_result_artifact_catalog_ids=selected,
    )


def default_catalog(*, canonical_text="事实A", evidence_ref_id=1, support_id="vc1"):
    return (
        entry(
            support_id,
            "VERIFIED_CLAIM",
            canonical_text=canonical_text,
            verdict="SUPPORTED",
            evidence_ref=vref(evid(evidence_ref_id)) if evidence_ref_id is not None else None,
        ),
    )


def default_sources(evidence_ref_id=1):
    return (
        CitationSourceV1(
            evidence_ref_id=evid(evidence_ref_id),
            source_domain="EXTERNAL_WEB",
            locator_kind="WEB",
            locator_value="https://example.com",
            preview=None,
        ),
    )


def empty_state():
    return CriterionEvaluationStateV1(
        completed_item_ids=(), adopted_outputs=(), constraint_checks=()
    )


class FinalCandidateValidatorTests(unittest.TestCase):
    def _validate(self, snap, cand, *, catalog=(), sources=(), state=None):
        return validate_final_candidate(
            snapshot=snap,
            candidate=cand,
            catalog=catalog,
            citation_sources=sources,
            state=state or empty_state(),
        )

    def test_accept_happy_path(self):
        snap = snapshot()
        cand = candidate(snap)
        result = self._validate(snap, cand, catalog=default_catalog(), sources=default_sources())
        self.assertIsInstance(result, FinalCandidateValidationV1)
        self.assertEqual(result.verdict, "ACCEPT")
        self.assertEqual(result.reason_codes, ())
        self.assertEqual(result.repair_paths, ())
        self.assertEqual(result.security_violations, ())
        self.assertEqual(result.snapshot_id, snap_id(snap))
        self.assertEqual(
            result.candidate_digest, canonical_json_sha256(cand.model_dump(mode="json"))
        )
        self.assertRegex(result.validation_digest, r"^[0-9a-f]{64}$")

    def test_snapshot_stale_binding_mismatch(self):
        snap = snapshot()
        cand = candidate(snap, snapshot_digest="1" * 64)
        result = self._validate(snap, cand, catalog=default_catalog(), sources=default_sources())
        self.assertEqual(result.verdict, "SNAPSHOT_STALE")
        self.assertEqual(result.reason_codes, ("FINAL_SYNTHESIS_SNAPSHOT_STALE",))

    def test_snapshot_stale_authorization_revoked(self):
        snap = snapshot(authorization_revoked=True)
        result = self._validate(
            snap, candidate(snap), catalog=default_catalog(), sources=default_sources()
        )
        self.assertEqual(result.verdict, "SNAPSHOT_STALE")
        self.assertEqual(result.reason_codes, ("AUTHORIZATION_REVOKED",))

    def test_unsafe_security_violation(self):
        snap = snapshot()
        cand = candidate(snap, title="<script>alert(1)</script>")
        result = self._validate(snap, cand, catalog=default_catalog(), sources=default_sources())
        self.assertEqual(result.verdict, "UNSAFE")
        self.assertEqual(result.reason_codes, ("FINAL_MODEL_OUTPUT_UNSAFE",))
        self.assertIn("SCRIPT", result.security_violations)

    def test_security_precedes_repairable(self):
        snap = snapshot()
        cand = candidate(snap, title="<script>x</script>")
        # 同时存在文本漂移，但安全扫描先于 Claim 支持 → UNSAFE。
        catalog = default_catalog(canonical_text="事实B")
        result = self._validate(snap, cand, catalog=catalog, sources=default_sources())
        self.assertEqual(result.verdict, "UNSAFE")

    def test_repairable_canonical_text_mismatch(self):
        snap = snapshot()
        catalog = default_catalog(canonical_text="事实B")
        result = self._validate(snap, candidate(snap), catalog=catalog, sources=default_sources())
        self.assertEqual(result.verdict, "REPAIRABLE_INVALID")
        self.assertEqual(result.reason_codes, ("CANONICAL_TEXT_MISMATCH",))
        self.assertEqual(result.repair_paths, ("claims/c1/text",))

    def test_repairable_claim_requires_citation(self):
        snap = snapshot()
        catalog = default_catalog(evidence_ref_id=None)
        result = self._validate(snap, candidate(snap), catalog=catalog, sources=())
        self.assertEqual(result.verdict, "REPAIRABLE_INVALID")
        self.assertEqual(result.reason_codes, ("CLAIM_REQUIRES_CITATION",))
        self.assertEqual(result.repair_paths, ("claims",))

    def test_repairable_unverified_code(self):
        snap = snapshot()
        sem = semantic_id(snap)
        claims = (claim("c1", "RECOMMENDATION", "建议", criteria=(sem,)),)
        sections = (
            section(
                "s1",
                FinalCodeBlockV1(
                    block_kind="CODE",
                    language="python",
                    code="print('x')",
                    support_catalog_ids=(),
                ),
            ),
        )
        assessments = (
            SemanticCriterionAssessmentV1(
                criterion_id=sem,
                assessment="SUPPORTED",
                supporting_catalog_ids=(),
                claim_local_ids=("c1",),
                note="ok",
            ),
        )
        cand = candidate(snap, claims=claims, sections=sections, assessments=assessments)
        result = self._validate(snap, cand, catalog=(), sources=())
        self.assertEqual(result.verdict, "REPAIRABLE_INVALID")
        self.assertEqual(result.reason_codes, ("UNVERIFIED_CODE",))
        self.assertEqual(result.repair_paths, ("sections/s1/blocks/0/code",))

    def test_repairable_artifact_link_not_selected(self):
        snap = snapshot()
        sections = (
            section(
                "s1",
                paragraph("c1"),
                FinalArtifactLinkBlockV1(
                    block_kind="ARTIFACT_LINK", catalog_id="art1", display_name="报告"
                ),
            ),
        )
        cand = candidate(snap, sections=sections)
        catalog = default_catalog() + (entry("art1", "ADOPTED_ARTIFACT", artifact_kind="REPORT"),)
        result = self._validate(snap, cand, catalog=catalog, sources=default_sources())
        self.assertEqual(result.verdict, "REPAIRABLE_INVALID")
        self.assertEqual(result.reason_codes, ("ARTIFACT_LINK_NOT_SELECTED",))

    def test_repairable_selected_artifact_not_linked(self):
        snap = snapshot()
        cand = candidate(snap, selected=("art1",))
        catalog = default_catalog() + (entry("art1", "ADOPTED_ARTIFACT", artifact_kind="REPORT"),)
        result = self._validate(snap, cand, catalog=catalog, sources=default_sources())
        self.assertEqual(result.verdict, "REPAIRABLE_INVALID")
        self.assertEqual(result.reason_codes, ("SELECTED_ARTIFACT_NOT_LINKED",))

    def test_output_contract_citations_required(self):
        snap = snapshot(output_contract=_contract(citations="REQUIRED"))
        sem = semantic_id(snap)
        claims = (claim("c1", "RECOMMENDATION", "建议", criteria=(sem,)),)
        assessments = (
            SemanticCriterionAssessmentV1(
                criterion_id=sem,
                assessment="SUPPORTED",
                supporting_catalog_ids=(),
                claim_local_ids=("c1",),
                note="ok",
            ),
        )
        result = self._validate(
            snap,
            candidate(snap, claims=claims, assessments=assessments),
            catalog=(),
            sources=(),
        )
        self.assertEqual(result.verdict, "REPAIRABLE_INVALID")
        self.assertEqual(result.reason_codes, ("OUTPUT_CONTRACT_CITATIONS_REQUIRED",))

    def test_output_contract_citations_forbidden(self):
        snap = snapshot(output_contract=_contract(citations="FORBIDDEN"))
        result = self._validate(
            snap, candidate(snap), catalog=default_catalog(), sources=default_sources()
        )
        self.assertEqual(result.verdict, "REPAIRABLE_INVALID")
        self.assertEqual(result.reason_codes, ("OUTPUT_CONTRACT_CITATIONS_FORBIDDEN",))

    def test_unrecoverable_size_limit(self):
        snap = snapshot()
        big = tuple(claim(f"c{i}", "RECOMMENDATION", "x" * 2000) for i in range(100))
        result = self._validate(
            snap, candidate(snap, claims=big), catalog=default_catalog(), sources=default_sources()
        )
        self.assertEqual(result.verdict, "UNRECOVERABLE_INVALID")
        self.assertEqual(result.reason_codes, ("CANDIDATE_SIZE_LIMIT_EXCEEDED",))

    def test_unrecoverable_dangling_claim_ref(self):
        snap = snapshot()
        cand = candidate(snap, sections=(section("s1", paragraph("nope")),))
        result = self._validate(snap, cand, catalog=default_catalog(), sources=default_sources())
        self.assertEqual(result.verdict, "UNRECOVERABLE_INVALID")
        self.assertEqual(result.reason_codes, ("CANDIDATE_CLAIM_REF_MISSING",))

    def test_unrecoverable_semantic_coverage_mismatch(self):
        snap = snapshot()
        result = self._validate(
            snap,
            candidate(snap, assessments=()),
            catalog=default_catalog(),
            sources=default_sources(),
        )
        self.assertEqual(result.verdict, "UNRECOVERABLE_INVALID")
        self.assertEqual(result.reason_codes, ("SEMANTIC_ASSESSMENT_COVERAGE_MISMATCH",))

    def test_unrecoverable_catalog_duplicate(self):
        snap = snapshot()
        dup = default_catalog() + default_catalog()
        result = self._validate(snap, candidate(snap), catalog=dup, sources=default_sources())
        self.assertEqual(result.verdict, "UNRECOVERABLE_INVALID")
        self.assertEqual(result.reason_codes, ("CATALOG_DUPLICATE_ID",))

    def test_unrecoverable_claim_support_ref_missing(self):
        snap = snapshot()
        claims = (claim("c1", "SOURCE_FACT", "事实A", support=("nope",)),)
        result = self._validate(
            snap,
            candidate(snap, claims=claims),
            catalog=default_catalog(),
            sources=default_sources(),
        )
        self.assertEqual(result.verdict, "UNRECOVERABLE_INVALID")
        self.assertEqual(result.reason_codes, ("CLAIM_SUPPORT_REF_MISSING",))


if __name__ == "__main__":
    unittest.main()
