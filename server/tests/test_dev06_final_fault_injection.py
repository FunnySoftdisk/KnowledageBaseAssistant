"""DEV-06 Final 综合编排故障注入离线验收：模型失败与依赖失败均失败关闭。

不接真实模型/PG/Blob：模型端口与提交端口注入故障替身。证明 `FinalSynthesisService.finalize()`
在模型候选生成抛错（结构修复耗尽 / 网关失败）或终态提交抛错（依赖故障）时均稳定传播、不产出
半截提交、不静默放宽 Schema——`FINAL_COMMITTED` 只在整链通过后才可达。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic_ai.exceptions import UnexpectedModelBehavior

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
    FinalSynthesisModelOutputV1,
    SemanticCriterionAssessmentV1,
    assemble_final_candidate,
)
from knowledge_system.modules.tasking.domain.final_citation import CitationSourceV1
from knowledge_system.modules.tasking.domain.final_evaluation import CriterionEvaluationStateV1
from knowledge_system.modules.tasking.domain.final_synthesis import (
    FinalProtectedBudgetV1,
    FinalSynthesisSnapshotV1,
    freeze_final_synthesis_snapshot,
)
from knowledge_system.modules.tasking.domain.input_contracts import OutputContractV1
from knowledge_system.modules.tasking.domain.planning_fragments import CriterionSourceKindV1

ZERO_SHA = "0" * 64
GENERATED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _vref(ref_id: UUID) -> VersionedRefV1:
    return VersionedRefV1(ref_id=ref_id, version=1, integrity_digest=ZERO_SHA)


def _criterion() -> CompiledCriterionV1:
    return CompiledCriterionV1(
        criterion_id=uuid4(),
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


def make_snapshot() -> FinalSynthesisSnapshotV1:
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
            compiled_criteria=(_criterion(),),
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
            degraded=False,
            final_protected_budget=FinalProtectedBudgetV1(
                model_requests=2, input_tokens=0, output_tokens=0
            ),
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
            builder_profile="final-synthesis-context-builder-v1",
            builder_schema_ref=SchemaRefV1(
                schema_id="final-synthesis", schema_version="v1", schema_digest=ZERO_SHA
            ),
            snapshot_digest=ZERO_SHA,
        )
    )


def make_catalog() -> tuple[FinalSupportCatalogEntryV1, ...]:
    return (
        FinalSupportCatalogEntryV1(
            catalog_id="vc1",
            kind="VERIFIED_CLAIM",
            source_ref=_vref(UUID("00000000-0000-4000-8000-000000000900")),
            canonical_text="事实A",
            verdict="SUPPORTED",
            artifact_kind=None,
            evidence_ref=_vref(UUID("00000000-0000-4000-8000-000000000001")),
        ),
    )


def make_sources() -> tuple[CitationSourceV1, ...]:
    return (
        CitationSourceV1(
            evidence_ref_id=UUID("00000000-0000-4000-8000-000000000001"),
            source_domain="EXTERNAL_WEB",
            locator_kind="WEB",
            locator_value="https://example.com",
            preview=None,
        ),
    )


def make_candidate(snap: FinalSynthesisSnapshotV1) -> FinalSynthesisCandidateV1:
    criterion_id = snap.compiled_criteria[0].criterion_id
    output = FinalSynthesisModelOutputV1(
        contract_version="final_synthesis_model_output_v1",
        title="报告标题",
        claims=(
            FinalClaimDraftV1(
                local_claim_id="c1",
                claim_kind="SOURCE_FACT",
                text="事实A",
                support_catalog_ids=("vc1",),
                criterion_ids=(),
                conflict_set_ids=(),
            ),
        ),
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
                assessment="SUPPORTED",
                supporting_catalog_ids=("vc1",),
                claim_local_ids=("c1",),
                note="证据支持",
            ),
        ),
        gap_reports=(),
        selected_result_artifact_catalog_ids=(),
    )
    return assemble_final_candidate(output, snap)


def make_materials(snap: FinalSynthesisSnapshotV1) -> FinalSynthesisMaterials:
    return FinalSynthesisMaterials(
        snapshot=snap,
        catalog=make_catalog(),
        citation_sources=make_sources(),
        evaluation_state=CriterionEvaluationStateV1(
            completed_item_ids=(), adopted_outputs=(), constraint_checks=()
        ),
        degradation_flags=(),
        output_contract_complete=True,
        final_attempt_id=uuid4(),
        candidate_artifact_id=uuid4(),
        validation_artifact_id=uuid4(),
        criterion_evaluation_set_artifact_id=uuid4(),
        completion_gate_decision_artifact_id=uuid4(),
        generated_at=GENERATED_AT,
    )


class FailingFinalModel:
    async def generate_candidate(self, snapshot, catalog, repair_hints=()):
        raise UnexpectedModelBehavior("model unavailable")


class RecordingCommitWriter:
    def __init__(self, *, raise_on_commit: bool = False) -> None:
        self._raise_on_commit = raise_on_commit
        self.commits: list[TaskResultCommitV1] = []

    async def commit(self, commit: TaskResultCommitV1) -> TaskResultCommitResult:
        self.commits.append(commit)
        if self._raise_on_commit:
            raise RuntimeError("DEPENDENCY_UNAVAILABLE")
        return TaskResultCommitResult(
            disposition=TaskResultCommitDisposition.INSERTED,
            task_id=commit.task_id,
            result_version=commit.result_version,
            result_commit_digest=commit.result_commit_digest,
            row_version=1,
        )


class FinalFaultInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_failure_fails_closed_without_commit(self) -> None:
        snap = make_snapshot()
        writer = RecordingCommitWriter()
        service = FinalSynthesisService(FailingFinalModel(), writer)
        with self.assertRaises(UnexpectedModelBehavior):
            await service.finalize(make_materials(snap))
        self.assertEqual(writer.commits, [])

    async def test_writer_failure_propagates_no_partial_state(self) -> None:
        snap = make_snapshot()

        class FixedModel:
            def __init__(self, candidate: FinalSynthesisCandidateV1) -> None:
                self._candidate = candidate

            async def generate_candidate(self, snapshot, catalog, repair_hints=()):
                return self._candidate

        writer = RecordingCommitWriter(raise_on_commit=True)
        service = FinalSynthesisService(FixedModel(make_candidate(snap)), writer)
        with self.assertRaisesRegex(RuntimeError, "DEPENDENCY_UNAVAILABLE"):
            await service.finalize(make_materials(snap))
        # 提交被尝试但依赖故障向上传播；服务端不吞掉错误、不伪装为终态。
        self.assertEqual(len(writer.commits), 1)


if __name__ == "__main__":
    unittest.main()
