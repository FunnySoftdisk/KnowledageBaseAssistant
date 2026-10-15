"""DEV-06：Final 综合编排 —— 把「模型候选 → 校验 → 求值 → 引用 → 完成判定 → 渲染 → 装配 → 提交」
串成一条零 I/O 垂直切片（成功 Final 路径）。

服务自身零 LLM、零 I/O：候选由注入的 `FinalSynthesisModelPort` 生成、终态提交由注入的
`TaskResultCommitWriter` 持久化；其余步骤全部调用已冻结的确定性领域函数
（`validate_final_candidate`/`evaluate_all_criteria`/`resolve_citations`/`decide_completion`/
`assemble_task_result`/`build_commit_receipt`）。校验返回 `REPAIRABLE_INVALID` 时按
`FinalProtectedBudgetV1.model_requests`（∈[1,2]）做有界结构修复重试；预算耗尽或非 `ACCEPT`
稳定失败关闭（携校验结果），不静默放宽 Schema。

映射函数 `build_task_result_commit` 只做「已装配结果 + 收据 + 快照 → `TaskResultCommitV1`」的
确定性投影，供重放稳定；`result_version` 固定 `1`（首版），`commit_receipt_id` 与
`final_snapshot_id` 由内容寻址派生。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from knowledge_system.modules.tasking.domain.final_admission import FinalSupportCatalogEntryV1
from knowledge_system.modules.tasking.domain.final_candidate import FinalSynthesisCandidateV1
from knowledge_system.modules.tasking.domain.final_citation import (
    CitationSourceV1,
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
    FINAL_ANSWER_RENDERER_PROFILE,
    FINAL_ANSWER_RENDERER_PROFILE_DIGEST,
)
from knowledge_system.modules.tasking.domain.final_result import (
    ResultDegradationFlagV1,
    TaskResultAssemblyV1,
    TaskResultCommitReceiptV1,
    assemble_task_result,
    build_commit_receipt,
    commit_receipt_id,
)
from knowledge_system.modules.tasking.domain.final_synthesis import (
    FinalSynthesisSnapshotV1,
    final_synthesis_snapshot_id,
)
from knowledge_system.modules.tasking.domain.final_validation import (
    FinalCandidateValidationV1,
    validate_final_candidate,
)
from knowledge_system.modules.tasking.domain.runtime_decision import FinalOutcomeKindV1

from .final_commit import (
    TaskResultCommitDisposition,
    TaskResultCommitResult,
    TaskResultCommitV1,
)

_FACT_KINDS = frozenset({"SOURCE_FACT", "DERIVED_CONCLUSION"})

FinalSynthesisOutcomeV1 = Literal[
    "FINAL_COMMITTED", "FINAL_CANDIDATE_REJECTED", "FINAL_COMMIT_CONFLICT"
]


class FinalSynthesisModelPort(Protocol):
    """Final 候选生成端口；实现负责把快照+目录序列化为 Prompt 并跑真实模型。

    `repair_hints` 为校验给出的有界修复路径，仅在结构修复重试时非空。
    """

    async def generate_candidate(
        self,
        snapshot: FinalSynthesisSnapshotV1,
        catalog: tuple[FinalSupportCatalogEntryV1, ...],
        repair_hints: tuple[str, ...] = (),
    ) -> FinalSynthesisCandidateV1: ...


class TaskResultCommitWriter(Protocol):
    """`workflow.task_result` 终态提交端口；实现负责开事务、调用 Repository 并提交。"""

    async def commit(self, commit: TaskResultCommitV1) -> TaskResultCommitResult: ...


@dataclass(frozen=True, slots=True)
class FinalSynthesisMaterials:
    """Final 综合需要的权威材料（由 `FinalSynthesisContextBuilderV1` 在 `START_FINAL_SYNTHESIS`
    事务内构建并冻结；各产物 Artifact ID 由上层在候选生成前落库，本切片只投影引用）。"""

    snapshot: FinalSynthesisSnapshotV1
    catalog: tuple[FinalSupportCatalogEntryV1, ...]
    citation_sources: tuple[CitationSourceV1, ...]
    evaluation_state: CriterionEvaluationStateV1
    degradation_flags: tuple[ResultDegradationFlagV1, ...]
    output_contract_complete: bool
    final_attempt_id: UUID
    candidate_artifact_id: UUID
    validation_artifact_id: UUID
    criterion_evaluation_set_artifact_id: UUID
    completion_gate_decision_artifact_id: UUID
    generated_at: datetime


@dataclass(frozen=True, slots=True)
class FinalSynthesisResult:
    outcome: FinalSynthesisOutcomeV1
    answer_status: FinalOutcomeKindV1 | None = None
    result_commit_digest: str | None = None
    result_version: int | None = None
    commit_disposition: TaskResultCommitDisposition | None = None
    validation: FinalCandidateValidationV1 | None = None


def _substantive_claims_supported(candidate: FinalSynthesisCandidateV1) -> bool:
    """至少一条实质（事实）Claim；校验 ACCEPT 已保证其全部受支持。"""

    return any(claim.claim_kind in _FACT_KINDS for claim in candidate.claims)


def build_task_result_commit(
    *,
    assembly: TaskResultAssemblyV1,
    receipt: TaskResultCommitReceiptV1,
    snapshot: FinalSynthesisSnapshotV1,
    decision: CompletionDecisionV1,
    candidate_artifact_id: UUID,
    validation_artifact_id: UUID,
    criterion_evaluation_set_artifact_id: UUID,
    completion_gate_decision_artifact_id: UUID,
    final_attempt_id: UUID,
) -> TaskResultCommitV1:
    """把已装配结果与收据投影为不可变 `TaskResultCommitV1`（`result_version` 固定 1）。"""

    result = assembly.result
    snapshot_id = final_synthesis_snapshot_id(
        task_id=snapshot.task_id,
        runtime_decision_id=snapshot.runtime_decision_id,
        snapshot_digest=snapshot.snapshot_digest,
        builder_profile=snapshot.builder_profile,
    )
    return TaskResultCommitV1(
        task_id=assembly.task_id,
        result_version=1,
        answer_status=decision.decision,
        answer_artifact_id=assembly.answer_id,
        answer_sha256=assembly.answer_sha256,
        result_envelope_artifact_id=assembly.envelope_id,
        citation_count=len(result.citations),
        result_artifact_count=len(result.result_artifacts),
        criterion_checks=result.criterion_checks,
        constraint_checks=result.constraint_checks,
        degradation_flags=tuple(flag.flag for flag in result.degradation_flags),
        final_snapshot_id=snapshot_id,
        final_attempt_id=final_attempt_id,
        candidate_artifact_id=candidate_artifact_id,
        validation_artifact_id=validation_artifact_id,
        criterion_evaluation_set_artifact_id=criterion_evaluation_set_artifact_id,
        completion_gate_decision_artifact_id=completion_gate_decision_artifact_id,
        renderer_profile=FINAL_ANSWER_RENDERER_PROFILE,
        renderer_profile_digest=FINAL_ANSWER_RENDERER_PROFILE_DIGEST,
        result_commit_digest=assembly.commit_digest,
        commit_receipt_id=commit_receipt_id(receipt),
        generated_at=assembly.generated_at,
    )


class FinalSynthesisService:
    """Final 综合垂直编排：候选 → 校验 → 求值 → 引用 → 判定 → 渲染 → 装配 → 提交。"""

    def __init__(self, model: FinalSynthesisModelPort, writer: TaskResultCommitWriter) -> None:
        self._model = model
        self._writer = writer

    async def finalize(self, materials: FinalSynthesisMaterials) -> FinalSynthesisResult:
        snapshot = materials.snapshot
        candidate = await self._model.generate_candidate(snapshot, materials.catalog)
        validation = self._validate(snapshot, candidate, materials)
        budget = snapshot.final_protected_budget.model_requests
        attempts = 1
        while validation.verdict == "REPAIRABLE_INVALID" and attempts < budget:
            candidate = await self._model.generate_candidate(
                snapshot, materials.catalog, repair_hints=validation.repair_paths
            )
            validation = self._validate(snapshot, candidate, materials)
            attempts += 1
        if validation.verdict != "ACCEPT":
            return FinalSynthesisResult(
                outcome="FINAL_CANDIDATE_REJECTED", validation=validation
            )

        evaluations = evaluate_all_criteria(
            compiled_criteria=snapshot.compiled_criteria,
            state=materials.evaluation_state,
            semantic_assessments=candidate.semantic_criterion_assessments,
        )
        decision = decide_completion(
            compiled_criteria=snapshot.compiled_criteria,
            evaluations=evaluations,
            constraint_checks=materials.evaluation_state.constraint_checks,
            output_contract_complete=materials.output_contract_complete,
            substantive_claims_supported=_substantive_claims_supported(candidate),
            has_critical_degradation=snapshot.degraded,
        )
        citations = resolve_citations(
            candidate.claims, materials.catalog, materials.citation_sources
        )
        assembly = assemble_task_result(
            task_id=snapshot.task_id,
            plan_version=snapshot.plan_version,
            work_version=snapshot.work_version,
            snapshot=snapshot,
            candidate=candidate,
            validation=validation,
            evaluations=evaluations,
            decision=decision,
            citations=citations,
            catalog=materials.catalog,
            output_contract=snapshot.output_contract,
            constraint_checks=materials.evaluation_state.constraint_checks,
            degradation_flags=materials.degradation_flags,
            generated_at=materials.generated_at,
        )
        receipt = build_commit_receipt(assembly=assembly, completed_at=materials.generated_at)
        commit = build_task_result_commit(
            assembly=assembly,
            receipt=receipt,
            snapshot=snapshot,
            decision=decision,
            candidate_artifact_id=materials.candidate_artifact_id,
            validation_artifact_id=materials.validation_artifact_id,
            criterion_evaluation_set_artifact_id=materials.criterion_evaluation_set_artifact_id,
            completion_gate_decision_artifact_id=materials.completion_gate_decision_artifact_id,
            final_attempt_id=materials.final_attempt_id,
        )
        commit_result = await self._writer.commit(commit)
        if commit_result.disposition == TaskResultCommitDisposition.CONFLICT:
            return FinalSynthesisResult(
                outcome="FINAL_COMMIT_CONFLICT",
                result_commit_digest=commit_result.result_commit_digest,
                result_version=commit_result.result_version,
                commit_disposition=commit_result.disposition,
            )
        return FinalSynthesisResult(
            outcome="FINAL_COMMITTED",
            answer_status=decision.decision,
            result_commit_digest=commit_result.result_commit_digest,
            result_version=commit_result.result_version,
            commit_disposition=commit_result.disposition,
        )

    @staticmethod
    def _validate(
        snapshot: FinalSynthesisSnapshotV1,
        candidate: FinalSynthesisCandidateV1,
        materials: FinalSynthesisMaterials,
    ) -> FinalCandidateValidationV1:
        return validate_final_candidate(
            snapshot=snapshot,
            candidate=candidate,
            catalog=materials.catalog,
            citation_sources=materials.citation_sources,
            state=materials.evaluation_state,
        )


__all__ = [
    "FinalSynthesisMaterials",
    "FinalSynthesisModelPort",
    "FinalSynthesisOutcomeV1",
    "FinalSynthesisResult",
    "FinalSynthesisService",
    "TaskResultCommitWriter",
    "build_task_result_commit",
]
