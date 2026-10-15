"""DEV-06 Final 综合编排离线验收：`FinalSynthesisService` 的签名盲测全维度门禁。

不接真实模型/PG/Blob：模型端口注入盲测替身（固定候选签名），提交端口注入记录替身。证明
`FINAL_COMMITTED` 的 COMPLETE/PARTIAL/INSUFFICIENT_EVIDENCE 三种终态、`REPAIRABLE_INVALID`
有界结构修复重试、预算耗尽或非 ACCEPT 稳定失败关闭、`FINAL_COMMIT_CONFLICT` 对账冲突、
`build_task_result_commit` 的确定性投影与整链 Commit Digest 的确定性。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from knowledge_system.modules.tasking.application.final_commit import (
    TaskResultCommitDisposition,
    TaskResultCommitResult,
    TaskResultCommitV1,
)
from knowledge_system.modules.tasking.application.final_synthesis import (
    FinalSynthesisMaterials,
    FinalSynthesisService,
)
from knowledge_system.modules.tasking.domain.compiled_criterion import (
    EVALUATOR_PROFILE_EVIDENCE_GROUNDED,
    CompiledCriterionV1,
    EvidenceSupportPredicateV1,
)
from knowledge_system.modules.tasking.domain.core_types import (
    PolicySnapshotRefV1,
    SchemaRefV1,
    VersionedRefV1,
)
from knowledge_system.modules.tasking.domain.final_admission import FinalSupportCatalogEntryV1
from knowledge_system.modules.tasking.domain.final_candidate import (
    FinalClaimDraftV1,
    FinalParagraphBlockV1,
    FinalSectionDraftV1,
    FinalSynthesisCandidateV1,
    SemanticCriterionAssessmentV1,
)
from knowledge_system.modules.tasking.domain.final_citation import CitationSourceV1
from knowledge_system.modules.tasking.domain.final_evaluation import CriterionEvaluationStateV1
from knowledge_system.modules.tasking.domain.final_render import (
    FINAL_ANSWER_RENDERER_PROFILE,
    FINAL_ANSWER_RENDERER_PROFILE_DIGEST,
)
from knowledge_system.modules.tasking.domain.final_synthesis import (
    FinalProtectedBudgetV1,
    FinalSynthesisSnapshotV1,
    final_synthesis_snapshot_id,
    freeze_final_synthesis_snapshot,
)
from knowledge_system.modules.tasking.domain.input_contracts import OutputContractV1
from knowledge_system.modules.tasking.domain.planning_fragments import CriterionSourceKindV1
from knowledge_system.modules.tasking.domain.runtime_decision import FinalOutcomeKindV1

ZERO_SHA = "0" * 64
GENERATED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _evid(i: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{i:012d}")


def _vref(ref_id: UUID) -> VersionedRefV1:
    return VersionedRefV1(ref_id=ref_id, version=1, integrity_digest=ZERO_SHA)


def _criterion(criterion_id: UUID | None = None) -> CompiledCriterionV1:
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


def _contract() -> OutputContractV1:
    return OutputContractV1(
        format="MARKDOWN",
        delivery="INLINE",
        language="zh-CN",
        schema_ref=None,
        template_or_skill_ref=None,
        citations="OPTIONAL",
        max_output_bytes=100_000,
    )


def _snapshot(*, criteria=None, model_requests=2, degraded=False) -> FinalSynthesisSnapshotV1:
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
            compiled_criteria=criteria if criteria is not None else (_criterion(),),
            effective_constraint_refs=(),
            output_contract=_contract(),
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
            degraded=degraded,
            final_protected_budget=FinalProtectedBudgetV1(
                model_requests=model_requests, input_tokens=0, output_tokens=0
            ),
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
            builder_profile="final-synthesis-context-builder-v1",
            builder_schema_ref=SchemaRefV1(
                schema_id="final-synthesis", schema_version="v1", schema_digest=ZERO_SHA
            ),
            snapshot_digest=ZERO_SHA,
        )
    )


def _snap_id(snap: FinalSynthesisSnapshotV1) -> UUID:
    return final_synthesis_snapshot_id(
        task_id=snap.task_id,
        runtime_decision_id=snap.runtime_decision_id,
        snapshot_digest=snap.snapshot_digest,
        builder_profile=snap.builder_profile,
    )


def _claim(local_id, kind, text, *, support=(), criteria=()) -> FinalClaimDraftV1:
    return FinalClaimDraftV1(
        local_claim_id=local_id,
        claim_kind=kind,
        text=text,
        support_catalog_ids=support,
        criterion_ids=criteria,
        conflict_set_ids=(),
    )


def _candidate(
    snap: FinalSynthesisSnapshotV1,
    *,
    claims=None,
    assessment_kind="SUPPORTED",
    snapshot_digest=None,
    title="报告标题",
) -> FinalSynthesisCandidateV1:
    criterion_id = snap.compiled_criteria[0].criterion_id
    return FinalSynthesisCandidateV1(
        contract_version="final_synthesis_v1",
        snapshot_id=_snap_id(snap),
        snapshot_digest=snapshot_digest if snapshot_digest is not None else snap.snapshot_digest,
        title=title,
        claims=claims
        if claims is not None
        else (_claim("c1", "SOURCE_FACT", "事实A", support=("vc1",)),),
        sections=(
            FinalSectionDraftV1(
                local_section_id="s1",
                heading="概述",
                blocks=(FinalParagraphBlockV1(block_kind="PARAGRAPH", claim_local_ids=("c1",)),),
            ),
        ),
        semantic_criterion_assessments=(
            SemanticCriterionAssessmentV1(
                criterion_id=criterion_id,
                assessment=assessment_kind,
                supporting_catalog_ids=("vc1",),
                claim_local_ids=("c1",),
                note="证据支持",
            ),
        ),
        gap_reports=(),
        selected_result_artifact_catalog_ids=(),
    )


def _catalog(canonical_text="事实A") -> tuple[FinalSupportCatalogEntryV1, ...]:
    return (
        FinalSupportCatalogEntryV1(
            catalog_id="vc1",
            kind="VERIFIED_CLAIM",
            source_ref=_vref(_evid(900)),
            canonical_text=canonical_text,
            verdict="SUPPORTED",
            artifact_kind=None,
            evidence_ref=_vref(_evid(1)),
        ),
    )


def _sources() -> tuple[CitationSourceV1, ...]:
    return (
        CitationSourceV1(
            evidence_ref_id=_evid(1),
            source_domain="EXTERNAL_WEB",
            locator_kind="WEB",
            locator_value="https://example.com",
            preview=None,
        ),
    )


def _state() -> CriterionEvaluationStateV1:
    return CriterionEvaluationStateV1(
        completed_item_ids=(), adopted_outputs=(), constraint_checks=()
    )


def _materials(
    snap: FinalSynthesisSnapshotV1,
    *,
    catalog=None,
    sources=None,
    state=None,
    output_contract_complete=True,
) -> FinalSynthesisMaterials:
    return FinalSynthesisMaterials(
        snapshot=snap,
        catalog=catalog if catalog is not None else _catalog(),
        citation_sources=sources if sources is not None else _sources(),
        evaluation_state=state if state is not None else _state(),
        degradation_flags=(),
        output_contract_complete=output_contract_complete,
        final_attempt_id=uuid4(),
        candidate_artifact_id=uuid4(),
        validation_artifact_id=uuid4(),
        criterion_evaluation_set_artifact_id=uuid4(),
        completion_gate_decision_artifact_id=uuid4(),
        generated_at=GENERATED_AT,
    )


class StubFinalModel:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.repair_hints_seen: list[tuple[str, ...]] = []

    async def generate_candidate(self, snapshot, catalog, repair_hints=()):
        self.calls += 1
        self.repair_hints_seen.append(tuple(repair_hints))
        index = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[index]


class RecordingCommitWriter:
    def __init__(self, disposition: str = "INSERTED"):
        self.disposition = TaskResultCommitDisposition(disposition)
        self.commits: list[TaskResultCommitV1] = []

    async def commit(self, commit: TaskResultCommitV1) -> TaskResultCommitResult:
        self.commits.append(commit)
        return TaskResultCommitResult(
            disposition=self.disposition,
            task_id=commit.task_id,
            result_version=commit.result_version,
            result_commit_digest=commit.result_commit_digest,
            row_version=1,
        )


class FinalSynthesisServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_commits(self):
        snap = _snapshot()
        model = StubFinalModel([_candidate(snap)])
        writer = RecordingCommitWriter()
        result = await FinalSynthesisService(model, writer).finalize(_materials(snap))

        self.assertEqual(result.outcome, "FINAL_COMMITTED")
        self.assertEqual(result.answer_status, FinalOutcomeKindV1.COMPLETE)
        self.assertEqual(result.commit_disposition, TaskResultCommitDisposition.INSERTED)
        self.assertEqual(result.result_version, 1)
        self.assertRegex(result.result_commit_digest, r"^[0-9a-f]{64}$")

        commit = writer.commits[0]
        self.assertEqual(commit.task_id, snap.task_id)
        self.assertEqual(commit.result_version, 1)
        self.assertEqual(commit.answer_status, FinalOutcomeKindV1.COMPLETE)
        self.assertEqual(commit.citation_count, 1)
        self.assertEqual(commit.result_artifact_count, 0)
        self.assertEqual(commit.renderer_profile, FINAL_ANSWER_RENDERER_PROFILE)
        self.assertEqual(commit.renderer_profile_digest, FINAL_ANSWER_RENDERER_PROFILE_DIGEST)
        self.assertEqual(commit.result_commit_digest, result.result_commit_digest)

    async def test_partial_commits(self):
        snap = _snapshot()
        model = StubFinalModel([_candidate(snap, assessment_kind="PARTIALLY_SUPPORTED")])
        result = await FinalSynthesisService(model, RecordingCommitWriter()).finalize(
            _materials(snap)
        )
        self.assertEqual(result.outcome, "FINAL_COMMITTED")
        self.assertEqual(result.answer_status, FinalOutcomeKindV1.PARTIAL)

    async def test_insufficient_evidence_commits(self):
        snap = _snapshot()
        recommendation = _claim(
            "c1", "RECOMMENDATION", "建议改进", criteria=(snap.compiled_criteria[0].criterion_id,)
        )
        candidate = _candidate(snap, claims=(recommendation,))
        model = StubFinalModel([candidate])
        result = await FinalSynthesisService(model, RecordingCommitWriter()).finalize(
            _materials(snap)
        )
        self.assertEqual(result.outcome, "FINAL_COMMITTED")
        self.assertEqual(result.answer_status, FinalOutcomeKindV1.INSUFFICIENT_EVIDENCE)

    async def test_repairable_invalid_retries_within_budget(self):
        snap = _snapshot(model_requests=2)
        drifted = _candidate(
            snap, claims=(_claim("c1", "SOURCE_FACT", "漂移文本", support=("vc1",)),)
        )
        fixed = _candidate(snap)
        model = StubFinalModel([drifted, fixed])
        result = await FinalSynthesisService(model, RecordingCommitWriter()).finalize(
            _materials(snap)
        )
        self.assertEqual(model.calls, 2)
        self.assertEqual(model.repair_hints_seen[1], ("claims/c1/text",))
        self.assertEqual(result.outcome, "FINAL_COMMITTED")
        self.assertEqual(result.answer_status, FinalOutcomeKindV1.COMPLETE)

    async def test_repairable_invalid_budget_exhausted_rejects(self):
        snap = _snapshot(model_requests=1)
        drifted = _candidate(
            snap, claims=(_claim("c1", "SOURCE_FACT", "漂移文本", support=("vc1",)),)
        )
        model = StubFinalModel([drifted])
        result = await FinalSynthesisService(model, RecordingCommitWriter()).finalize(
            _materials(snap)
        )
        self.assertEqual(model.calls, 1)
        self.assertEqual(result.outcome, "FINAL_CANDIDATE_REJECTED")
        self.assertEqual(result.validation.verdict, "REPAIRABLE_INVALID")

    async def test_snapshot_stale_rejects(self):
        snap = _snapshot()
        stale = _candidate(snap, snapshot_digest="1" * 64)
        result = await FinalSynthesisService(
            StubFinalModel([stale]), RecordingCommitWriter()
        ).finalize(_materials(snap))
        self.assertEqual(result.outcome, "FINAL_CANDIDATE_REJECTED")
        self.assertEqual(result.validation.verdict, "SNAPSHOT_STALE")

    async def test_commit_conflict_surfaces(self):
        snap = _snapshot()
        result = await FinalSynthesisService(
            StubFinalModel([_candidate(snap)]), RecordingCommitWriter("CONFLICT")
        ).finalize(_materials(snap))
        self.assertEqual(result.outcome, "FINAL_COMMIT_CONFLICT")
        self.assertEqual(result.commit_disposition, TaskResultCommitDisposition.CONFLICT)

    async def test_deterministic_commit_digest(self):
        snap = _snapshot()
        candidate = _candidate(snap)
        first = await FinalSynthesisService(
            StubFinalModel([candidate]), RecordingCommitWriter()
        ).finalize(_materials(snap))
        second = await FinalSynthesisService(
            StubFinalModel([candidate]), RecordingCommitWriter()
        ).finalize(_materials(snap))
        self.assertEqual(first.result_commit_digest, second.result_commit_digest)

    async def test_commit_digest_changes_with_candidate(self):
        snap = _snapshot()
        first = await FinalSynthesisService(
            StubFinalModel([_candidate(snap)]), RecordingCommitWriter()
        ).finalize(_materials(snap))
        second = await FinalSynthesisService(
            StubFinalModel([_candidate(snap, title="另一标题")]), RecordingCommitWriter()
        ).finalize(_materials(snap))
        self.assertNotEqual(first.result_commit_digest, second.result_commit_digest)


if __name__ == "__main__":
    unittest.main()
