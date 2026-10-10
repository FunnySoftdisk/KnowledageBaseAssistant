"""AGT-08 §5.34：`task_result_v1` 契约 + 零 LLM `TaskResultAssemblerV1` 与终态提交收据。

本模块落地 §5.34 的确定性装配：Answer/Citation/Checks/Envelope 以 RFC 8785 JCS 计算摘要，
Answer/Envelope 内容寻址 ID 为 `UUIDv5(TASK_RESULT_ARTIFACT_NAMESPACE, task_id:plan_version:
KIND:sha256)`；Commit Digest 覆盖 Task/版本、Snapshot、Candidate、Validation、Evaluation Set、
Completion Decision、Renderer Profile、Answer/Envelope 与 Result Artifact Set 摘要。`generated_at`
在首次 Assembly 由调用方固定，重试复用同一值以保持幂等。

Staging Blob 写入、条件写、不可见性与 §5.34 终态事务锁序（Conversation -> Task -> … -> Budget
reservation）属于持久层/编排切片，本切片不落地；这里只固化 `TaskResultAssemblyV1` 与
`TaskResultCommitReceiptV1` 的不可变契约、摘要与冲突/重放的确定性输入。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import rfc8785
from pydantic import Field, StringConstraints, field_validator

from .compiled_criterion import (
    CompiledCriterionV1,
    CompletionRoleV1,
    EvidenceSupportPredicateV1,
)
from .core_types import (
    Sha256Hex,
    StableKey,
    StrictContractV1,
    VersionedRefV1,
    canonical_json_sha256,
)
from .final_admission import FinalResultArtifactKindV1, FinalSupportCatalogEntryV1
from .final_candidate import (
    FinalArtifactLinkBlockV1,
    FinalClaimDraftV1,
    FinalSynthesisCandidateV1,
    SemanticCriterionAssessmentV1,
)
from .final_citation import CitationId, FinalCitationV1, claim_citation_ids
from .final_gate import (
    CompletionDecisionV1,
    ConstraintCheckV1,
    CriterionEvaluationStatusV1,
    CriterionEvaluationV1,
)
from .final_render import FINAL_ANSWER_RENDERER_PROFILE, FinalAnswerV1, render_final_answer
from .final_synthesis import FinalSynthesisSnapshotV1
from .final_validation import FinalCandidateValidationV1
from .input_contracts import OutputContractV1

TASK_RESULT_ARTIFACT_NAMESPACE = uuid5(
    NAMESPACE_URL, "urn:knowledge-system:task-result-artifact:v1"
)
ENVELOPE_MAX_BYTES = 2_097_152  # 2 MiB


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC_DATETIME_REQUIRED")
    return value.astimezone(UTC)


class ResultCriterionCheckV1(StrictContractV1):
    """Criterion Check：仅公开 Criterion ID、Completion Role、五态、显示摘要与 Result Citation。"""

    criterion_id: UUID
    completion_role: CompletionRoleV1
    status: CriterionEvaluationStatusV1
    display_summary: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    citation_ids: tuple[CitationId, ...] = Field(max_length=16)


class ResultDegradationFlagV1(StrictContractV1):
    flag: StableKey
    detail: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class ResultArtifactEntryV1(StrictContractV1):
    catalog_id: StableKey
    artifact_kind: FinalResultArtifactKindV1
    artifact_ref: VersionedRefV1
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class TaskResultV1(StrictContractV1):
    """`task_result_v1`：Answer、Citation、Criterion/Constraint Check、降级与 Result Artifact。"""

    contract_version: Literal["task_result_v1"]
    task_id: UUID
    plan_version: int = Field(ge=1)
    work_version: int = Field(ge=1)
    answer: FinalAnswerV1
    citations: tuple[FinalCitationV1, ...] = Field(max_length=200)
    criterion_checks: tuple[ResultCriterionCheckV1, ...] = Field(max_length=12)
    constraint_checks: tuple[ConstraintCheckV1, ...] = Field(max_length=32)
    degradation_flags: tuple[ResultDegradationFlagV1, ...] = Field(max_length=16)
    result_artifacts: tuple[ResultArtifactEntryV1, ...] = Field(max_length=50)
    result_digest: Sha256Hex = "0" * 64


class TaskResultEnvelopeV1(StrictContractV1):
    """Envelope：Answer/Checks/Artifact 摘要与元数据；不含 commit_digest（由 Assembly 层绑定）。"""

    contract_version: Literal["task_result_envelope_v1"]
    task_id: UUID
    plan_version: int = Field(ge=1)
    work_version: int = Field(ge=1)
    answer_sha256: Sha256Hex
    citation_digest: Sha256Hex
    criterion_check_digest: Sha256Hex
    constraint_check_digest: Sha256Hex
    degradation_flag_digest: Sha256Hex
    result_artifact_set_digest: Sha256Hex
    generated_at: datetime
    envelope_digest: Sha256Hex = "0" * 64

    @field_validator("generated_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class TaskResultAssemblyV1(StrictContractV1):
    """不可变装配记录：Answer/Envelope 内容寻址 ID、Commit Digest 与首次装配时间。"""

    contract_version: Literal["task_result_assembly_v1"]
    task_id: UUID
    plan_version: int = Field(ge=1)
    work_version: int = Field(ge=1)
    result: TaskResultV1
    answer_id: UUID
    envelope_id: UUID
    answer_sha256: Sha256Hex
    envelope_sha256: Sha256Hex
    commit_digest: Sha256Hex
    generated_at: datetime
    assembly_digest: Sha256Hex = "0" * 64

    @field_validator("generated_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class TaskResultCommitReceiptV1(StrictContractV1):
    """终态提交收据：唯一 Commit Digest 绑定 Answer/Envelope 与 Result Artifact Set。"""

    contract_version: Literal["task_result_commit_receipt_v1"]
    task_id: UUID
    plan_version: int = Field(ge=1)
    work_version: int = Field(ge=1)
    commit_digest: Sha256Hex
    answer_id: UUID
    envelope_id: UUID
    result_artifact_ids: tuple[UUID, ...] = Field(max_length=50)
    completed_at: datetime
    receipt_digest: Sha256Hex = "0" * 64

    @field_validator("completed_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


def task_result_artifact_id(
    *, task_id: UUID, plan_version: int, kind: Literal["ANSWER", "ENVELOPE"], sha256: Sha256Hex
) -> UUID:
    """同一 (task, plan_version, kind, sha256) 派生同一内容寻址 ID。"""

    name = f"{str(task_id).lower()}:{plan_version}:{kind}:{sha256}"
    return uuid5(TASK_RESULT_ARTIFACT_NAMESPACE, name)


def freeze_task_result(result: TaskResultV1) -> TaskResultV1:
    digest = canonical_json_sha256(result.model_dump(mode="json", exclude={"result_digest"}))
    return result.model_copy(update={"result_digest": digest})


def freeze_envelope(envelope: TaskResultEnvelopeV1) -> TaskResultEnvelopeV1:
    digest = canonical_json_sha256(envelope.model_dump(mode="json", exclude={"envelope_digest"}))
    frozen = envelope.model_copy(update={"envelope_digest": digest})
    if len(rfc8785.dumps(frozen.model_dump(mode="json"))) > ENVELOPE_MAX_BYTES:
        raise ValueError("ENVELOPE_SIZE_LIMIT_EXCEEDED")
    return frozen


def freeze_assembly(assembly: TaskResultAssemblyV1) -> TaskResultAssemblyV1:
    digest = canonical_json_sha256(assembly.model_dump(mode="json", exclude={"assembly_digest"}))
    return assembly.model_copy(update={"assembly_digest": digest})


def compute_commit_digest(
    *,
    task_id: UUID,
    plan_version: int,
    work_version: int,
    snapshot: FinalSynthesisSnapshotV1,
    candidate: FinalSynthesisCandidateV1,
    validation: FinalCandidateValidationV1,
    evaluations: tuple[CriterionEvaluationV1, ...],
    decision: CompletionDecisionV1,
    answer_sha256: Sha256Hex,
    envelope_sha256: Sha256Hex,
    result_artifact_set_digest: Sha256Hex,
) -> Sha256Hex:
    """Commit Digest 覆盖 §5.34 全部绑定项：Task/版本、Snapshot、Candidate、Validation、
    Evaluation Set、Completion Decision、Renderer、Answer/Envelope 与 Result Artifact Set。"""

    payload = {
        "task_id": str(task_id).lower(),
        "plan_version": plan_version,
        "work_version": work_version,
        "snapshot_digest": snapshot.snapshot_digest,
        "candidate_digest": canonical_json_sha256(candidate.model_dump(mode="json")),
        "validation_digest": validation.validation_digest,
        "evaluation_set_digest": canonical_json_sha256(
            [evaluation.model_dump(mode="json") for evaluation in evaluations]
        ),
        "completion_decision_digest": decision.decision_digest,
        "renderer_profile": FINAL_ANSWER_RENDERER_PROFILE,
        "answer_sha256": answer_sha256,
        "envelope_sha256": envelope_sha256,
        "result_artifact_set_digest": result_artifact_set_digest,
    }
    return canonical_json_sha256(payload)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _criterion_citation_ids(
    criterion: CompiledCriterionV1,
    assessment: SemanticCriterionAssessmentV1 | None,
    claims_by_id: dict[str, FinalClaimDraftV1],
    catalog: tuple[FinalSupportCatalogEntryV1, ...],
    citations: tuple[FinalCitationV1, ...],
) -> tuple[str, ...]:
    if not isinstance(criterion.predicate, EvidenceSupportPredicateV1) or assessment is None:
        return ()
    ids: list[str] = []
    for local_id in assessment.claim_local_ids:
        for citation_id in claim_citation_ids(claims_by_id[local_id], catalog, citations):
            if citation_id not in ids:
                ids.append(citation_id)
    return tuple(ids)


def _build_criterion_checks(
    snapshot: FinalSynthesisSnapshotV1,
    candidate: FinalSynthesisCandidateV1,
    evaluations: tuple[CriterionEvaluationV1, ...],
    catalog: tuple[FinalSupportCatalogEntryV1, ...],
    citations: tuple[FinalCitationV1, ...],
) -> tuple[ResultCriterionCheckV1, ...]:
    criterion_ids = [criterion.criterion_id for criterion in snapshot.compiled_criteria]
    if len(set(criterion_ids)) != len(criterion_ids):
        raise ValueError("COMPILED_CRITERION_DUPLICATE")
    evaluation_by_id = {evaluation.criterion_id: evaluation for evaluation in evaluations}
    if len(evaluation_by_id) != len(evaluations):
        raise ValueError("EVALUATION_DUPLICATE")
    if set(evaluation_by_id) != set(criterion_ids):
        raise ValueError("EVALUATION_COVERAGE_MISMATCH")

    assessment_by_id = {
        assessment.criterion_id: assessment
        for assessment in candidate.semantic_criterion_assessments
    }
    claims_by_id = {claim.local_claim_id: claim for claim in candidate.claims}

    checks: list[ResultCriterionCheckV1] = []
    for criterion in snapshot.compiled_criteria:
        assessment = assessment_by_id.get(criterion.criterion_id)
        checks.append(
            ResultCriterionCheckV1(
                criterion_id=criterion.criterion_id,
                completion_role=criterion.completion_role,
                status=evaluation_by_id[criterion.criterion_id].status,
                display_summary=_truncate(criterion.statement, 512),
                citation_ids=_criterion_citation_ids(
                    criterion, assessment, claims_by_id, catalog, citations
                ),
            )
        )
    return tuple(checks)


def _build_result_artifacts(
    candidate: FinalSynthesisCandidateV1, catalog: tuple[FinalSupportCatalogEntryV1, ...]
) -> tuple[ResultArtifactEntryV1, ...]:
    catalog_by_id = {entry.catalog_id: entry for entry in catalog}
    display_by_catalog = {
        block.catalog_id: block.display_name
        for section in candidate.sections
        for block in section.blocks
        if isinstance(block, FinalArtifactLinkBlockV1)
    }
    entries: list[ResultArtifactEntryV1] = []
    for catalog_id in candidate.selected_result_artifact_catalog_ids:
        entry = catalog_by_id.get(catalog_id)
        if entry is None or entry.kind != "ADOPTED_ARTIFACT" or entry.artifact_kind is None:
            raise ValueError("RESULT_ARTIFACT_INVALID")
        entries.append(
            ResultArtifactEntryV1(
                catalog_id=catalog_id,
                artifact_kind=entry.artifact_kind,
                artifact_ref=entry.source_ref,
                display_name=display_by_catalog.get(catalog_id, catalog_id),
            )
        )
    return tuple(entries)


def assemble_task_result(
    *,
    task_id: UUID,
    plan_version: int,
    work_version: int,
    snapshot: FinalSynthesisSnapshotV1,
    candidate: FinalSynthesisCandidateV1,
    validation: FinalCandidateValidationV1,
    evaluations: tuple[CriterionEvaluationV1, ...],
    decision: CompletionDecisionV1,
    citations: tuple[FinalCitationV1, ...],
    catalog: tuple[FinalSupportCatalogEntryV1, ...],
    output_contract: OutputContractV1,
    constraint_checks: tuple[ConstraintCheckV1, ...],
    degradation_flags: tuple[ResultDegradationFlagV1, ...],
    generated_at: datetime,
) -> TaskResultAssemblyV1:
    """确定性装配 Answer/Citation/Checks/Envelope，输出不可变 `TaskResultAssemblyV1`。"""

    if validation.verdict != "ACCEPT":
        raise ValueError("FINAL_CANDIDATE_NOT_ACCEPTED")

    answer = render_final_answer(
        candidate=candidate,
        decision=decision,
        citations=citations,
        catalog=catalog,
        output_contract=output_contract,
    )
    criterion_checks = _build_criterion_checks(snapshot, candidate, evaluations, catalog, citations)
    result_artifacts = _build_result_artifacts(candidate, catalog)

    result = freeze_task_result(
        TaskResultV1(
            contract_version="task_result_v1",
            task_id=task_id,
            plan_version=plan_version,
            work_version=work_version,
            answer=answer,
            citations=citations,
            criterion_checks=criterion_checks,
            constraint_checks=constraint_checks,
            degradation_flags=degradation_flags,
            result_artifacts=result_artifacts,
            result_digest="0" * 64,
        )
    )

    citation_digest = canonical_json_sha256(
        [citation.model_dump(mode="json") for citation in citations]
    )
    criterion_check_digest = canonical_json_sha256(
        [check.model_dump(mode="json") for check in criterion_checks]
    )
    constraint_check_digest = canonical_json_sha256(
        [check.model_dump(mode="json") for check in constraint_checks]
    )
    degradation_flag_digest = canonical_json_sha256(
        [flag.model_dump(mode="json") for flag in degradation_flags]
    )
    result_artifact_set_digest = canonical_json_sha256(
        [artifact.model_dump(mode="json") for artifact in result_artifacts]
    )

    envelope = freeze_envelope(
        TaskResultEnvelopeV1(
            contract_version="task_result_envelope_v1",
            task_id=task_id,
            plan_version=plan_version,
            work_version=work_version,
            answer_sha256=answer.answer_digest,
            citation_digest=citation_digest,
            criterion_check_digest=criterion_check_digest,
            constraint_check_digest=constraint_check_digest,
            degradation_flag_digest=degradation_flag_digest,
            result_artifact_set_digest=result_artifact_set_digest,
            generated_at=generated_at,
            envelope_digest="0" * 64,
        )
    )

    answer_id = task_result_artifact_id(
        task_id=task_id, plan_version=plan_version, kind="ANSWER", sha256=answer.answer_digest
    )
    envelope_id = task_result_artifact_id(
        task_id=task_id,
        plan_version=plan_version,
        kind="ENVELOPE",
        sha256=envelope.envelope_digest,
    )
    commit_digest = compute_commit_digest(
        task_id=task_id,
        plan_version=plan_version,
        work_version=work_version,
        snapshot=snapshot,
        candidate=candidate,
        validation=validation,
        evaluations=evaluations,
        decision=decision,
        answer_sha256=answer.answer_digest,
        envelope_sha256=envelope.envelope_digest,
        result_artifact_set_digest=result_artifact_set_digest,
    )

    assembly = freeze_assembly(
        TaskResultAssemblyV1(
            contract_version="task_result_assembly_v1",
            task_id=task_id,
            plan_version=plan_version,
            work_version=work_version,
            result=result,
            answer_id=answer_id,
            envelope_id=envelope_id,
            answer_sha256=answer.answer_digest,
            envelope_sha256=envelope.envelope_digest,
            commit_digest=commit_digest,
            generated_at=generated_at,
            assembly_digest="0" * 64,
        )
    )
    return assembly


def build_commit_receipt(
    *, assembly: TaskResultAssemblyV1, completed_at: datetime
) -> TaskResultCommitReceiptV1:
    """从已装配结果构造终态提交收据；同一 Assembly + completed_at 派生同一收据。"""

    artifact_ids = tuple(
        artifact.artifact_ref.ref_id for artifact in assembly.result.result_artifacts
    )
    receipt = TaskResultCommitReceiptV1(
        contract_version="task_result_commit_receipt_v1",
        task_id=assembly.task_id,
        plan_version=assembly.plan_version,
        work_version=assembly.work_version,
        commit_digest=assembly.commit_digest,
        answer_id=assembly.answer_id,
        envelope_id=assembly.envelope_id,
        result_artifact_ids=artifact_ids,
        completed_at=completed_at,
        receipt_digest="0" * 64,
    )
    digest = canonical_json_sha256(receipt.model_dump(mode="json", exclude={"receipt_digest"}))
    return receipt.model_copy(update={"receipt_digest": digest})


__all__ = [
    "ENVELOPE_MAX_BYTES",
    "TASK_RESULT_ARTIFACT_NAMESPACE",
    "ResultArtifactEntryV1",
    "ResultCriterionCheckV1",
    "ResultDegradationFlagV1",
    "TaskResultAssemblyV1",
    "TaskResultCommitReceiptV1",
    "TaskResultEnvelopeV1",
    "TaskResultV1",
    "assemble_task_result",
    "build_commit_receipt",
    "compute_commit_digest",
    "freeze_assembly",
    "freeze_envelope",
    "freeze_task_result",
    "task_result_artifact_id",
]
