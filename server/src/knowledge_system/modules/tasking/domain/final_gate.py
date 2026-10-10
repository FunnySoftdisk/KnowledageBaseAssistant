"""AGT-08 §5.29-5.30：零 LLM 完成判定核心。

`CompletionGateV1` 依据已编译 Criterion、逐项 `CriterionEvaluationV1`、约束检查、Output
Contract、实质 Claim 支持与降级事实，按固定真值表给出 `COMPLETE/PARTIAL/INSUFFICIENT_EVIDENCE`；
不使用模型 confidence、分数或可配置权重。`DeliverableUsabilityValidatorV1` 把完成判定映射为
交付物可用性四类。

确定性谓词（ARTIFACT_CONTRACT/STATE_FACT/CONSTRAINT）的逐项求值由 `CriterionEvaluatorRegistryV1`
在后续切片实现；本切片只固化评估契约、语义评估映射与判定真值表。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field

from .compiled_criterion import CompiledCriterionV1
from .core_types import (
    Sha256Hex,
    StrictContractV1,
    VersionedRefV1,
    canonical_json_sha256,
)
from .final_candidate import SemanticCriterionAssessmentKindV1
from .runtime_decision import FinalOutcomeKindV1


class CriterionEvaluationStatusV1(StrEnum):
    SATISFIED = "SATISFIED"
    PARTIAL = "PARTIAL"
    UNSATISFIED = "UNSATISFIED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class CriterionEvaluationV1(StrictContractV1):
    criterion_id: UUID
    status: CriterionEvaluationStatusV1
    evidence_refs: tuple[VersionedRefV1, ...] = Field(max_length=16)
    evaluation_digest: Sha256Hex = "0" * 64


class ConstraintCheckV1(StrictContractV1):
    constraint_id: UUID
    status: Literal["SATISFIED", "UNSATISFIED", "UNKNOWN"]


class CompletionDecisionV1(StrictContractV1):
    contract_version: Literal["completion_decision_v1"]
    decision: FinalOutcomeKindV1
    unmet_required_criterion_ids: tuple[UUID, ...]
    unmet_constraint_ids: tuple[UUID, ...]
    decision_digest: Sha256Hex = "0" * 64


DeliverableUsabilityV1 = Literal[
    "USABLE_COMPLETE", "USABLE_SUBSET", "GAP_REPORT_ONLY", "INVALID_CANDIDATE"
]

_SEMANTIC_TO_STATUS: dict[str, CriterionEvaluationStatusV1] = {
    "SUPPORTED": CriterionEvaluationStatusV1.SATISFIED,
    "PARTIALLY_SUPPORTED": CriterionEvaluationStatusV1.PARTIAL,
    "UNSUPPORTED": CriterionEvaluationStatusV1.UNSATISFIED,
    "UNKNOWN": CriterionEvaluationStatusV1.UNKNOWN,
}


def semantic_assessment_status(
    assessment: SemanticCriterionAssessmentKindV1,
) -> CriterionEvaluationStatusV1:
    """EVIDENCE_SUPPORT 语义评估 → 确定性求值状态的唯一映射。"""

    return _SEMANTIC_TO_STATUS[assessment]


def criterion_evaluation_digest(evaluation: CriterionEvaluationV1) -> str:
    return canonical_json_sha256(evaluation.model_dump(mode="json", exclude={"evaluation_digest"}))


def freeze_criterion_evaluation(
    evaluation: CriterionEvaluationV1,
) -> CriterionEvaluationV1:
    return evaluation.model_copy(
        update={"evaluation_digest": criterion_evaluation_digest(evaluation)}
    )


def decide_completion(
    *,
    compiled_criteria: tuple[CompiledCriterionV1, ...],
    evaluations: tuple[CriterionEvaluationV1, ...],
    constraint_checks: tuple[ConstraintCheckV1, ...],
    output_contract_complete: bool,
    substantive_claims_supported: bool,
    has_critical_degradation: bool,
) -> CompletionDecisionV1:
    """按 §5.30 真值表求唯一完成状态，失败关闭。"""

    criterion_by_id = {criterion.criterion_id: criterion for criterion in compiled_criteria}
    if len(criterion_by_id) != len(compiled_criteria):
        raise ValueError("COMPLETION_DUPLICATE_CRITERION")
    evaluation_by_id = {evaluation.criterion_id: evaluation for evaluation in evaluations}
    if len(evaluation_by_id) != len(evaluations):
        raise ValueError("COMPLETION_DUPLICATE_EVALUATION")
    if set(evaluation_by_id) != set(criterion_by_id):
        raise ValueError("COMPLETION_EVALUATION_COVERAGE_MISMATCH")

    applicable = [
        criterion
        for criterion in compiled_criteria
        if evaluation_by_id[criterion.criterion_id].status
        != CriterionEvaluationStatusV1.NOT_APPLICABLE
    ]
    core = [
        criterion for criterion in applicable if criterion.completion_role == "CORE_DELIVERABLE"
    ]
    required_auxiliary = [
        criterion for criterion in applicable if criterion.completion_role == "REQUIRED_AUXILIARY"
    ]
    if not core:
        raise ValueError("COMPLETION_NO_APPLICABLE_CORE")

    unmet_required_criteria = [
        criterion.criterion_id
        for criterion in (*core, *required_auxiliary)
        if evaluation_by_id[criterion.criterion_id].status != CriterionEvaluationStatusV1.SATISFIED
    ]
    unmet_constraints = [
        check.constraint_id for check in constraint_checks if check.status != "SATISFIED"
    ]

    fully_satisfied = (
        not unmet_required_criteria
        and not unmet_constraints
        and output_contract_complete
        and substantive_claims_supported
        and not has_critical_degradation
    )

    if fully_satisfied:
        decision = FinalOutcomeKindV1.COMPLETE
    elif substantive_claims_supported and any(
        evaluation_by_id[criterion.criterion_id].status
        in (CriterionEvaluationStatusV1.SATISFIED, CriterionEvaluationStatusV1.PARTIAL)
        for criterion in core
    ):
        decision = FinalOutcomeKindV1.PARTIAL
    else:
        decision = FinalOutcomeKindV1.INSUFFICIENT_EVIDENCE

    decision_record = CompletionDecisionV1(
        contract_version="completion_decision_v1",
        decision=decision,
        unmet_required_criterion_ids=tuple(unmet_required_criteria),
        unmet_constraint_ids=tuple(unmet_constraints),
        decision_digest="0" * 64,
    )
    digest = canonical_json_sha256(
        decision_record.model_dump(mode="json", exclude={"decision_digest"})
    )
    return decision_record.model_copy(update={"decision_digest": digest})


def deliverable_usability(
    *,
    decision: FinalOutcomeKindV1 | None,
    candidate_has_content: bool,
) -> DeliverableUsabilityV1:
    """把完成判定映射为交付物可用性；无判定或无内容即 INVALID_CANDIDATE。"""

    if decision is None or not candidate_has_content:
        return "INVALID_CANDIDATE"
    if decision == FinalOutcomeKindV1.COMPLETE:
        return "USABLE_COMPLETE"
    if decision == FinalOutcomeKindV1.PARTIAL:
        return "USABLE_SUBSET"
    return "GAP_REPORT_ONLY"


__all__ = [
    "CompletionDecisionV1",
    "ConstraintCheckV1",
    "CriterionEvaluationStatusV1",
    "CriterionEvaluationV1",
    "DeliverableUsabilityV1",
    "criterion_evaluation_digest",
    "decide_completion",
    "deliverable_usability",
    "freeze_criterion_evaluation",
    "semantic_assessment_status",
]
