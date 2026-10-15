"""DEV-06 Final 候选生成模型：候选内容子集、装配与 PydanticAI 适配器。

纯离线验收：模型端口以 `FunctionModel`/Fake 替身驱动，不接真实模型、不接 PG/Temporal；
结构修复（≤2 次）以 PydanticAI `retries` 验证，超出预算失败关闭不静默放宽 Schema。验证
`FinalSynthesisModelOutputV1` 候选内容子集不含快照身份、`assemble_final_candidate` 注入
`snapshot_id`/`snapshot_digest`，以及 `PydanticFinalModel` 在 `FinalSynthesisService` 中的
端口装配。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import ValidationError
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from knowledge_system.infrastructure.final_model import PydanticFinalModel
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
    FinalSynthesisModelOutputV1,
    SemanticCriterionAssessmentV1,
    assemble_final_candidate,
)
from knowledge_system.modules.tasking.domain.final_citation import CitationSourceV1
from knowledge_system.modules.tasking.domain.final_evaluation import CriterionEvaluationStateV1
from knowledge_system.modules.tasking.domain.final_synthesis import (
    FinalProtectedBudgetV1,
    FinalSynthesisSnapshotV1,
    final_synthesis_snapshot_id,
    freeze_final_synthesis_snapshot,
)
from knowledge_system.modules.tasking.domain.input_contracts import OutputContractV1
from knowledge_system.modules.tasking.domain.planning_fragments import CriterionSourceKindV1
from knowledge_system.modules.tasking.domain.runtime_decision import FinalOutcomeKindV1
from knowledge_system.modules.tasking.domain.supervisor_profiles import STRUCTURAL_REPAIR_LIMIT

ZERO_SHA = "0" * 64
GENERATED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _evid(i: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{i:012d}")


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


def _snap_id(snap: FinalSynthesisSnapshotV1) -> UUID:
    return final_synthesis_snapshot_id(
        task_id=snap.task_id,
        runtime_decision_id=snap.runtime_decision_id,
        snapshot_digest=snap.snapshot_digest,
        builder_profile=snap.builder_profile,
    )


def make_catalog(canonical_text: str = "事实A") -> tuple[FinalSupportCatalogEntryV1, ...]:
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


def make_sources() -> tuple[CitationSourceV1, ...]:
    return (
        CitationSourceV1(
            evidence_ref_id=_evid(1),
            source_domain="EXTERNAL_WEB",
            locator_kind="WEB",
            locator_value="https://example.com",
            preview=None,
        ),
    )


def make_output(
    snap: FinalSynthesisSnapshotV1, *, title: str = "报告标题"
) -> FinalSynthesisModelOutputV1:
    criterion_id = snap.compiled_criteria[0].criterion_id
    return FinalSynthesisModelOutputV1(
        contract_version="final_synthesis_model_output_v1",
        title=title,
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
                blocks=(
                    FinalParagraphBlockV1(
                        block_kind="PARAGRAPH", claim_local_ids=("c1",)
                    ),
                ),
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


class CandidateOutputTests(unittest.TestCase):
    def test_output_rejects_missing_section_and_gap(self) -> None:
        snap = make_snapshot()
        data = make_output(snap).model_dump()
        data["sections"] = ()
        data["gap_reports"] = ()
        with self.assertRaises(ValidationError):
            FinalSynthesisModelOutputV1.model_validate(data)

    def test_output_rejects_duplicate_claim_ids(self) -> None:
        snap = make_snapshot()
        output = make_output(snap)
        claim = output.claims[0].model_dump()
        data = output.model_dump()
        data["claims"] = (claim, claim)
        with self.assertRaises(ValidationError):
            FinalSynthesisModelOutputV1.model_validate(data)

    def test_output_has_no_snapshot_identity_fields(self) -> None:
        # 候选内容子集不得暴露快照身份/摘要字段，防止模型声明权威身份。
        self.assertNotIn("snapshot_id", FinalSynthesisModelOutputV1.model_fields)
        self.assertNotIn("snapshot_digest", FinalSynthesisModelOutputV1.model_fields)


class AssemblyTests(unittest.TestCase):
    def test_assemble_injects_snapshot_identity(self) -> None:
        snap = make_snapshot()
        output = make_output(snap)
        candidate = assemble_final_candidate(output, snap)
        self.assertEqual(candidate.snapshot_id, _snap_id(snap))
        self.assertEqual(candidate.snapshot_digest, snap.snapshot_digest)
        self.assertEqual(candidate.contract_version, "final_synthesis_v1")
        self.assertEqual(candidate.title, output.title)
        self.assertEqual(candidate.claims, output.claims)
        self.assertEqual(candidate.sections, output.sections)
        self.assertEqual(
            candidate.semantic_criterion_assessments, output.semantic_criterion_assessments
        )

    def test_assemble_is_deterministic_for_same_snapshot(self) -> None:
        snap = make_snapshot()
        output = make_output(snap)
        first = assemble_final_candidate(output, snap)
        second = assemble_final_candidate(output, snap)
        self.assertEqual(first, second)


class PydanticFinalModelTests(unittest.IsolatedAsyncioTestCase):
    def _responder(self, payload: str):
        async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.assertTrue(messages)
            self.assertEqual(info.function_tools, [])
            return ModelResponse([TextPart(payload)])

        return respond

    async def test_round_trip_assembles_candidate(self) -> None:
        snap = make_snapshot()
        expected = make_output(snap)
        model = PydanticFinalModel(FunctionModel(self._responder(expected.model_dump_json())))
        candidate = await model.generate_candidate(snap, make_catalog())
        self.assertEqual(candidate.snapshot_id, _snap_id(snap))
        self.assertEqual(candidate.snapshot_digest, snap.snapshot_digest)
        self.assertEqual(candidate.title, expected.title)

    async def test_repair_hints_reach_serialized_prompt(self) -> None:
        snap = make_snapshot()
        expected = make_output(snap)
        seen: list[str] = []

        async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            for part in messages[0].parts:
                seen.append(part.content)
            return ModelResponse([TextPart(expected.model_dump_json())])

        model = PydanticFinalModel(FunctionModel(respond))
        await model.generate_candidate(snap, make_catalog(), repair_hints=("claims/c1/text",))
        self.assertTrue(seen)
        self.assertIn("claims/c1/text", seen[0])

    async def test_structural_repair_recovers_within_budget(self) -> None:
        snap = make_snapshot()
        valid = make_output(snap)
        calls = 0

        async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                return ModelResponse([TextPart('{"not":"valid"}')])
            return ModelResponse([TextPart(valid.model_dump_json())])

        model = PydanticFinalModel(FunctionModel(respond))
        candidate = await model.generate_candidate(snap, make_catalog())
        self.assertEqual(candidate.title, valid.title)
        self.assertEqual(calls, 2)

    async def test_structural_repair_exhaustion_fails_closed(self) -> None:
        snap = make_snapshot()
        calls = 0

        async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal calls
            calls += 1
            return ModelResponse([TextPart('{"title":123}')])

        model = PydanticFinalModel(FunctionModel(respond))
        with self.assertRaises(UnexpectedModelBehavior):
            await model.generate_candidate(snap, make_catalog())
        self.assertEqual(calls, STRUCTURAL_REPAIR_LIMIT + 1)

    async def test_adapter_satisfies_port_in_service(self) -> None:
        snap = make_snapshot()
        expected = make_output(snap)
        model = PydanticFinalModel(FunctionModel(self._responder(expected.model_dump_json())))
        writer = RecordingCommitWriter()
        result = await FinalSynthesisService(model, writer).finalize(make_materials(snap))
        self.assertEqual(result.outcome, "FINAL_COMMITTED")
        self.assertEqual(result.answer_status, FinalOutcomeKindV1.COMPLETE)
        self.assertEqual(len(writer.commits), 1)


if __name__ == "__main__":
    unittest.main()
