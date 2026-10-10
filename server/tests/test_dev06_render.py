"""DEV-06 Final 离线验收：`FinalAnswerRendererV1` 渲染与 `TaskResultAssemblerV1` 装配。

不接真实模型/PG/Blob；只证明 §5.34 的确定性渲染（UTF-8/LF/NFC、标题/段落/列表/表格/代码/文件
清单、`[c:c_001]` Marker、非 COMPLETE 强制「限制与未完成项」、转义与无临时 URL）与装配（Answer/
Citation/Checks/Envelope 的 JCS 摘要、内容寻址 UUIDv5 ID、覆盖 Task/版本/Snapshot/Candidate/
Validation/Evaluation Set/Completion Decision/Renderer/Answer/Envelope/Result Artifact Set 的
Commit Digest、首次装配时间固定、终态提交收据）。
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
    FinalListBlockV1,
    FinalParagraphBlockV1,
    FinalSectionDraftV1,
    FinalSynthesisCandidateV1,
    FinalTableBlockV1,
    SemanticCriterionAssessmentV1,
)
from knowledge_system.modules.tasking.domain.final_citation import (
    CitationSourceV1,
    FinalCitationV1,
    resolve_citations,
)
from knowledge_system.modules.tasking.domain.final_evaluation import (
    CriterionEvaluationStateV1,
    evaluate_all_criteria,
)
from knowledge_system.modules.tasking.domain.final_gate import (
    CompletionDecisionV1,
    decide_completion,
)
from knowledge_system.modules.tasking.domain.final_render import (
    FinalAnswerV1,
    render_final_answer,
)
from knowledge_system.modules.tasking.domain.final_result import (
    ResultCriterionCheckV1,
    TaskResultAssemblyV1,
    TaskResultCommitReceiptV1,
    assemble_task_result,
    build_commit_receipt,
    compute_commit_digest,
    task_result_artifact_id,
)
from knowledge_system.modules.tasking.domain.final_synthesis import (
    FinalProtectedBudgetV1,
    FinalSynthesisSnapshotV1,
    final_synthesis_snapshot_id,
    freeze_final_synthesis_snapshot,
)
from knowledge_system.modules.tasking.domain.final_validation import validate_final_candidate
from knowledge_system.modules.tasking.domain.input_contracts import OutputContractV1
from knowledge_system.modules.tasking.domain.planning_fragments import CriterionSourceKindV1
from knowledge_system.modules.tasking.domain.runtime_decision import FinalOutcomeKindV1

ZERO_SHA = "0" * 64
GENERATED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


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
        completion_role="CORE_DELIVERABLE",
        protected=True,
        evaluation_mode="EVIDENCE_GROUNDED_SEMANTIC",
        predicate=EvidenceSupportPredicateV1(
            kind="EVIDENCE_SUPPORT", producer_plan_item_id=uuid4(), min_citations=1
        ),
        applicability=None,
        evaluator_profile=EVALUATOR_PROFILE_EVIDENCE_GROUNDED,
        digest=ZERO_SHA,
    )


def _contract(citations="OPTIONAL", language="zh-CN", format="MARKDOWN"):
    return OutputContractV1(
        format=format,
        delivery="INLINE",
        language=language,
        schema_ref=None,
        template_or_skill_ref=None,
        citations=citations,
        max_output_bytes=100_000,
    )


def snapshot(*, compiled_criteria=None, output_contract=None):
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
            authorization_revoked=False,
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


def snap_id(snap):
    return final_synthesis_snapshot_id(
        task_id=snap.task_id,
        runtime_decision_id=snap.runtime_decision_id,
        snapshot_digest=snap.snapshot_digest,
        builder_profile=snap.builder_profile,
    )


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
    sem = next(c.criterion_id for c in snap.compiled_criteria)
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


def _citation(citation_id, evidence_id):
    return FinalCitationV1(
        citation_id=citation_id,
        evidence_ref_id=evid(evidence_id),
        source_domain="EXTERNAL_WEB",
        locator_kind="WEB",
        locator_value="https://example.com",
        preview=None,
    )


def _complete_decision():
    return CompletionDecisionV1(
        contract_version="completion_decision_v1",
        decision=FinalOutcomeKindV1.COMPLETE,
        unmet_required_criterion_ids=(),
        unmet_constraint_ids=(),
        decision_digest=ZERO_SHA,
    )


def _pipeline(snap, cand, catalog, sources):
    """完整运行校验/引用/求值/完成判定，得到 ACCEPT 装配所需的确定性输入。"""

    validation = validate_final_candidate(
        snapshot=snap,
        candidate=cand,
        catalog=catalog,
        citation_sources=sources,
        state=empty_state(),
    )
    citations = resolve_citations(cand.claims, catalog, sources)
    evaluations = evaluate_all_criteria(
        compiled_criteria=snap.compiled_criteria,
        state=empty_state(),
        semantic_assessments=cand.semantic_criterion_assessments,
    )
    decision = decide_completion(
        compiled_criteria=snap.compiled_criteria,
        evaluations=evaluations,
        constraint_checks=(),
        output_contract_complete=True,
        substantive_claims_supported=True,
        has_critical_degradation=False,
    )
    return validation, evaluations, decision, citations


class FinalAnswerRendererTests(unittest.TestCase):
    def _render(self, cand, *, decision=None, citations=(), catalog=(), language="zh-CN"):
        return render_final_answer(
            candidate=cand,
            decision=decision or _complete_decision(),
            citations=citations,
            catalog=catalog,
            output_contract=_contract(language=language),
        )

    def test_render_complete_happy_path(self):
        snap = snapshot()
        result = self._render(
            candidate(snap),
            citations=(_citation("c_001", 1),),
            catalog=default_catalog(),
        )
        self.assertIsInstance(result, FinalAnswerV1)
        self.assertEqual(result.format, "MARKDOWN")
        self.assertEqual(result.language, "zh-CN")
        self.assertEqual(result.markdown, "# 报告标题\n\n## 概述\n\n事实A [c:c_001]")
        self.assertRegex(result.answer_digest, r"^[0-9a-f]{64}$")

    def test_render_escapes_markdown(self):
        snap = snapshot()
        claims = (claim("c1", "SOURCE_FACT", "使用 # 标记与 *强调*", support=("vc1",)),)
        result = self._render(
            candidate(snap, claims=claims),
            citations=(_citation("c_001", 1),),
            catalog=default_catalog(canonical_text="使用 # 标记与 *强调*"),
        )
        self.assertIn(r"使用 \# 标记与 \*强调\* [c:c_001]", result.markdown)

    def test_render_fact_claim_marker_only(self):
        snap = snapshot()
        claims = (claim("c1", "RECOMMENDATION", "建议改进", criteria=(uuid4(),)),)
        result = self._render(
            candidate(snap, claims=claims, assessments=()),
            citations=(_citation("c_001", 1),),
            catalog=(),
        )
        self.assertNotIn("[c:", result.markdown)

    def test_render_ordered_and_unordered_list(self):
        snap = snapshot()
        claims = (
            claim("c1", "SOURCE_FACT", "事实A", support=("vc1",)),
            claim("c2", "SOURCE_FACT", "事实B", support=("vc1",)),
        )
        sections = (
            section(
                "s1",
                FinalListBlockV1(block_kind="BULLET_LIST", ordered=True, items=(("c1",), ("c2",))),
            ),
        )
        result = self._render(
            candidate(snap, claims=claims, sections=sections),
            citations=(_citation("c_001", 1),),
            catalog=default_catalog(),
        )
        self.assertIn("1. 事实A [c:c_001]", result.markdown)
        self.assertIn("2. 事实B [c:c_001]", result.markdown)

    def test_render_table(self):
        snap = snapshot()
        claims = (
            claim("c1", "SOURCE_FACT", "事实A", support=("vc1",)),
            claim("c2", "SOURCE_FACT", "事实B", support=("vc1",)),
        )
        table = FinalTableBlockV1(
            block_kind="TABLE",
            column_count=2,
            rows=((("c1",), ("c2",)), (("c1",), ("c2",))),
        )
        result = self._render(
            candidate(snap, claims=claims, sections=(section("s1", table),)),
            citations=(_citation("c_001", 1),),
            catalog=default_catalog(),
        )
        self.assertIn("| 事实A [c:c_001] | 事实B [c:c_001] |", result.markdown)
        self.assertIn("| --- | --- |", result.markdown)

    def test_render_code_block(self):
        snap = snapshot()
        code = FinalCodeBlockV1(
            block_kind="CODE", language="python", code="print('x')", support_catalog_ids=()
        )
        result = self._render(
            candidate(snap, sections=(section("s1", code),)),
            citations=(_citation("c_001", 1),),
            catalog=default_catalog(),
        )
        self.assertIn("```python\nprint('x')\n```", result.markdown)

    def test_render_artifact_file_list(self):
        snap = snapshot()
        sections = (
            section(
                "s1",
                paragraph("c1"),
                FinalArtifactLinkBlockV1(
                    block_kind="ARTIFACT_LINK", catalog_id="art1", display_name="报告附件"
                ),
            ),
        )
        catalog = default_catalog() + (entry("art1", "ADOPTED_ARTIFACT", artifact_kind="REPORT"),)
        result = self._render(
            candidate(snap, sections=sections, selected=("art1",)),
            citations=(_citation("c_001", 1),),
            catalog=catalog,
        )
        self.assertIn("## 结果制品", result.markdown)
        self.assertIn("- 报告附件（REPORT）", result.markdown)
        self.assertNotIn("http", result.markdown)

    def test_render_non_complete_appends_limitations(self):
        snap = snapshot()
        from knowledge_system.modules.tasking.domain.final_candidate import FinalGapReportV1

        cand = candidate(snap)
        cand = cand.model_copy(
            update={
                "gap_reports": (
                    FinalGapReportV1(
                        local_gap_id="g1",
                        gap_kind="EVIDENCE_MISSING",
                        description="缺少证据",
                        source_gap_ids=(),
                        affected_criterion_ids=(),
                        next_steps=("补充证据",),
                    ),
                )
            }
        )
        decision = CompletionDecisionV1(
            contract_version="completion_decision_v1",
            decision=FinalOutcomeKindV1.PARTIAL,
            unmet_required_criterion_ids=(uuid4(),),
            unmet_constraint_ids=(),
            decision_digest=ZERO_SHA,
        )
        result = self._render(
            cand, decision=decision, citations=(_citation("c_001", 1),), catalog=default_catalog()
        )
        self.assertIn("## 限制与未完成项", result.markdown)
        self.assertIn("- 缺少证据", result.markdown)
        self.assertIn("  - 补充证据", result.markdown)

    def test_render_normalizes_lf(self):
        snap = snapshot()
        claims = (claim("c1", "SOURCE_FACT", "第一行\r\n第二行", support=("vc1",)),)
        result = self._render(
            candidate(snap, claims=claims),
            citations=(_citation("c_001", 1),),
            catalog=default_catalog(canonical_text="第一行\n第二行"),
        )
        self.assertNotIn("\r", result.markdown)
        self.assertIn("第一行\n第二行", result.markdown)

    def test_render_deterministic_digest(self):
        snap = snapshot()
        kwargs = dict(citations=(_citation("c_001", 1),), catalog=default_catalog())
        first = self._render(candidate(snap), **kwargs)
        second = self._render(candidate(snap), **kwargs)
        self.assertEqual(first.answer_digest, second.answer_digest)

    def test_render_unsupported_json_format(self):
        snap = snapshot()
        json_contract = OutputContractV1(
            format="JSON",
            delivery="INLINE",
            language="zh-CN",
            schema_ref=SchemaRefV1(schema_id="result", schema_version="v1", schema_digest=ZERO_SHA),
            template_or_skill_ref=None,
            citations="OPTIONAL",
            max_output_bytes=100_000,
        )
        with self.assertRaises(ValueError):
            render_final_answer(
                candidate=candidate(snap),
                decision=_complete_decision(),
                citations=(),
                catalog=default_catalog(),
                output_contract=json_contract,
            )


class TaskResultAssemblerTests(unittest.TestCase):
    def _assemble(self, snap, cand, *, catalog=(), sources=(), generated_at=GENERATED_AT):
        validation, evaluations, decision, citations = _pipeline(snap, cand, catalog, sources)
        return assemble_task_result(
            task_id=snap.task_id,
            plan_version=snap.plan_version,
            work_version=snap.work_version,
            snapshot=snap,
            candidate=cand,
            validation=validation,
            evaluations=evaluations,
            decision=decision,
            citations=citations,
            catalog=catalog,
            output_contract=snap.output_contract,
            constraint_checks=(),
            degradation_flags=(),
            generated_at=generated_at,
        )

    def test_assemble_happy_path(self):
        snap = snapshot()
        cand = candidate(snap)
        assembly = self._assemble(snap, cand, catalog=default_catalog(), sources=default_sources())
        self.assertIsInstance(assembly, TaskResultAssemblyV1)
        self.assertEqual(assembly.task_id, snap.task_id)
        self.assertEqual(assembly.plan_version, 1)
        self.assertEqual(assembly.work_version, 1)
        self.assertEqual(assembly.generated_at, GENERATED_AT)
        self.assertRegex(assembly.commit_digest, r"^[0-9a-f]{64}$")
        self.assertRegex(assembly.assembly_digest, r"^[0-9a-f]{64}$")

        answer = assembly.result.answer
        self.assertEqual(
            assembly.answer_id,
            task_result_artifact_id(
                task_id=snap.task_id,
                plan_version=1,
                kind="ANSWER",
                sha256=answer.answer_digest,
            ),
        )
        self.assertEqual(
            assembly.envelope_id,
            task_result_artifact_id(
                task_id=snap.task_id,
                plan_version=1,
                kind="ENVELOPE",
                sha256=assembly.envelope_sha256,
            ),
        )
        self.assertEqual(assembly.answer_sha256, answer.answer_digest)

        checks = assembly.result.criterion_checks
        self.assertEqual(len(checks), 1)
        self.assertIsInstance(checks[0], ResultCriterionCheckV1)
        self.assertEqual(checks[0].completion_role, "CORE_DELIVERABLE")
        self.assertEqual(checks[0].citation_ids, ("c_001",))

    def test_commit_digest_changes_with_candidate(self):
        snap = snapshot()
        first = self._assemble(
            snap, candidate(snap), catalog=default_catalog(), sources=default_sources()
        )
        altered = candidate(snap, title="另一标题")
        second = self._assemble(snap, altered, catalog=default_catalog(), sources=default_sources())
        self.assertNotEqual(first.commit_digest, second.commit_digest)

    def test_commit_digest_covers_evaluation_set(self):
        snap = snapshot()
        cand = candidate(snap)
        validation, evaluations, decision, citations = _pipeline(
            snap, cand, default_catalog(), default_sources()
        )
        answer = render_final_answer(
            candidate=cand,
            decision=decision,
            citations=citations,
            catalog=default_catalog(),
            output_contract=snap.output_contract,
        )
        digest = compute_commit_digest(
            task_id=snap.task_id,
            plan_version=1,
            work_version=1,
            snapshot=snap,
            candidate=cand,
            validation=validation,
            evaluations=evaluations,
            decision=decision,
            answer_sha256=answer.answer_digest,
            envelope_sha256=ZERO_SHA,
            result_artifact_set_digest=canonical_json_sha256([]),
        )
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_task_result_artifact_id_distinct_and_deterministic(self):
        task_id = uuid4()
        answer_id = task_result_artifact_id(
            task_id=task_id, plan_version=1, kind="ANSWER", sha256=ZERO_SHA
        )
        envelope_id = task_result_artifact_id(
            task_id=task_id, plan_version=1, kind="ENVELOPE", sha256=ZERO_SHA
        )
        self.assertNotEqual(answer_id, envelope_id)
        self.assertEqual(
            answer_id,
            task_result_artifact_id(
                task_id=task_id, plan_version=1, kind="ANSWER", sha256=ZERO_SHA
            ),
        )

    def test_assemble_rejects_non_accepted(self):
        snap = snapshot()
        cand = candidate(snap, snapshot_digest="1" * 64)
        with self.assertRaises(ValueError):
            self._assemble(snap, cand, catalog=default_catalog(), sources=default_sources())

    def test_build_commit_receipt(self):
        snap = snapshot()
        assembly = self._assemble(
            snap, candidate(snap), catalog=default_catalog(), sources=default_sources()
        )
        completed_at = datetime(2026, 1, 1, 13, 0, 0, tzinfo=UTC)
        receipt = build_commit_receipt(assembly=assembly, completed_at=completed_at)
        self.assertIsInstance(receipt, TaskResultCommitReceiptV1)
        self.assertEqual(receipt.commit_digest, assembly.commit_digest)
        self.assertEqual(receipt.answer_id, assembly.answer_id)
        self.assertEqual(receipt.envelope_id, assembly.envelope_id)
        self.assertEqual(receipt.completed_at, completed_at)
        self.assertRegex(receipt.receipt_digest, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
