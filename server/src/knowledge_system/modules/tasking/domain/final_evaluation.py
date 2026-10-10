"""AGT-08 §5.29：零 LLM `CriterionEvaluatorRegistryV1` 确定性谓词求值。

前四类 Predicate 中 `ARTIFACT_CONTRACT/STATE_FACT/CONSTRAINT` 由本模块确定性求值；
`EVIDENCE_SUPPORT` 使用证据约束的语义评估（由模型候选的 `SemanticCriterionAssessmentV1` 提供），
不在本模块求值；`USER_FACT` 为预留谓词，v1 编译器不产生。

求值只读取不可变状态投影 `CriterionEvaluationStateV1`（PG 权威事实的信号投影），不读取
正文/Prompt/模型输出，也不做 I/O 或授权。evidence_refs 由运行期以实际 VersionedRefV1 记录充实，
本切片只固化确定性状态。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field, model_validator

from .compiled_criterion import (
    ArtifactContractPredicateV1,
    CompiledCriterionV1,
    ConstraintPredicateV1,
    EvidenceSupportPredicateV1,
    StateFactPredicateV1,
)
from .core_types import StableKey, StrictContractV1
from .final_candidate import SemanticCriterionAssessmentV1
from .final_gate import (
    ConstraintCheckV1,
    CriterionEvaluationStatusV1,
    CriterionEvaluationV1,
    freeze_criterion_evaluation,
    semantic_assessment_status,
)


class AdoptedArtifactFactV1(StrictContractV1):
    producer_plan_item_id: UUID
    output_schema_key: StableKey


class CriterionEvaluationStateV1(StrictContractV1):
    """确定性谓词求值所需的 PG 事实投影；不含正文/Prompt/模型输出。"""

    completed_item_ids: tuple[UUID, ...] = Field(max_length=12)
    adopted_outputs: tuple[AdoptedArtifactFactV1, ...] = Field(max_length=50)
    constraint_checks: tuple[ConstraintCheckV1, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def validate_unique(self) -> CriterionEvaluationStateV1:
        if len(set(self.completed_item_ids)) != len(self.completed_item_ids):
            raise ValueError("COMPLETED_ITEM_DUPLICATE")
        if len(set(self.adopted_outputs)) != len(self.adopted_outputs):
            raise ValueError("ADOPTED_OUTPUT_DUPLICATE")
        constraint_ids = [check.constraint_id for check in self.constraint_checks]
        if len(set(constraint_ids)) != len(constraint_ids):
            raise ValueError("CONSTRAINT_CHECK_DUPLICATE")
        return self


_CONSTRAINT_STATUS_MAP = {
    "SATISFIED": CriterionEvaluationStatusV1.SATISFIED,
    "UNSATISFIED": CriterionEvaluationStatusV1.UNSATISFIED,
    "UNKNOWN": CriterionEvaluationStatusV1.UNKNOWN,
}


def _is_semantic(criterion: CompiledCriterionV1) -> bool:
    return isinstance(criterion.predicate, EvidenceSupportPredicateV1)


def _deterministic_status(
    criterion: CompiledCriterionV1, state: CriterionEvaluationStateV1
) -> CriterionEvaluationStatusV1:
    predicate = criterion.predicate
    if isinstance(predicate, ArtifactContractPredicateV1):
        fact = AdoptedArtifactFactV1(
            producer_plan_item_id=predicate.producer_plan_item_id,
            output_schema_key=predicate.output_schema_key,
        )
        return (
            CriterionEvaluationStatusV1.SATISFIED
            if fact in state.adopted_outputs
            else CriterionEvaluationStatusV1.UNSATISFIED
        )
    if isinstance(predicate, StateFactPredicateV1):
        return (
            CriterionEvaluationStatusV1.SATISFIED
            if predicate.subject_plan_item_id in state.completed_item_ids
            else CriterionEvaluationStatusV1.UNSATISFIED
        )
    if isinstance(predicate, ConstraintPredicateV1):
        by_id = {check.constraint_id: check for check in state.constraint_checks}
        check = by_id.get(predicate.constraint_ref.constraint_id)
        if check is None:
            raise ValueError("CRITERION_CONSTRAINT_CHECK_MISSING")
        return _CONSTRAINT_STATUS_MAP[check.status]
    raise ValueError("CRITERION_NOT_DETERMINISTICALLY_EVALUABLE")


def evaluate_criterion(
    criterion: CompiledCriterionV1, state: CriterionEvaluationStateV1
) -> CriterionEvaluationV1:
    """逐项确定性求值；EVIDENCE_SUPPORT/USER_FACT 失败关闭。"""

    if _is_semantic(criterion):
        raise ValueError("CRITERION_NOT_DETERMINISTICALLY_EVALUABLE")
    return freeze_criterion_evaluation(
        CriterionEvaluationV1(
            criterion_id=criterion.criterion_id,
            status=_deterministic_status(criterion, state),
            evidence_refs=(),
        )
    )


def evaluate_all_criteria(
    *,
    compiled_criteria: tuple[CompiledCriterionV1, ...],
    state: CriterionEvaluationStateV1,
    semantic_assessments: tuple[SemanticCriterionAssessmentV1, ...],
) -> tuple[CriterionEvaluationV1, ...]:
    """合并确定性求值与语义评估，覆盖全部 Compiled Criterion（失败关闭）。"""

    semantic_criterion_ids = {
        criterion.criterion_id for criterion in compiled_criteria if _is_semantic(criterion)
    }
    assessment_by_id = {assessment.criterion_id: assessment for assessment in semantic_assessments}
    if len(assessment_by_id) != len(semantic_assessments):
        raise ValueError("SEMANTIC_ASSESSMENT_DUPLICATE")
    if set(assessment_by_id) != semantic_criterion_ids:
        raise ValueError("SEMANTIC_ASSESSMENT_COVERAGE_MISMATCH")

    evaluations: list[CriterionEvaluationV1] = []
    for criterion in compiled_criteria:
        if _is_semantic(criterion):
            status = semantic_assessment_status(assessment_by_id[criterion.criterion_id].assessment)
        else:
            status = _deterministic_status(criterion, state)
        evaluations.append(
            freeze_criterion_evaluation(
                CriterionEvaluationV1(
                    criterion_id=criterion.criterion_id, status=status, evidence_refs=()
                )
            )
        )
    return tuple(evaluations)


__all__ = [
    "AdoptedArtifactFactV1",
    "CriterionEvaluationStateV1",
    "evaluate_all_criteria",
    "evaluate_criterion",
]
