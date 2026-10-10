"""CORE-38：零LLM `CriterionCompilerV1`，把Draft编译为不可变`CompiledCriterionV1`。

`CriterionDraftV1`只是模型草稿；`target`是`ARTIFACT/CITATION/STATE/USER`封闭联合并携带可解析
对象，`evidence_kind`由target派生而非自由字段。本模块只做确定性编译：target→严格Predicate、
来源规则生成的`source_kind/requirement_level/protected/completion_role/evaluator_profile`、稳定
`criterion_id`与Criterion Digest。模型不能设置或弱化这些字段。

首版Predicate只允许`ARTIFACT_CONTRACT/STATE_FACT/USER_FACT/CONSTRAINT/EVIDENCE_SUPPORT`；
前四类由`CriterionEvaluatorRegistryV1`确定性求值，只有`EVIDENCE_SUPPORT`使用证据约束的语义
评估。`USER_FACT`为预留谓词，v1编译器不产生（没有target映射到它）。`OUTPUT_CONTRACT/POLICY`
来源不在Draft可推导范围内，由调用方显式提供`source_kind/source_refs`。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, TypeAdapter, model_validator

from .core_types import (
    ConstraintRefV1,
    Sha256Hex,
    StableKey,
    StrictContractV1,
    canonical_json_sha256,
)
from .planning_fragments import (
    ArtifactCriterionTargetV1,
    CitationCriterionTargetV1,
    CriterionDraftV1,
    CriterionSourceKindV1,
    StateCriterionTargetV1,
    UserCriterionTargetV1,
    compiled_criterion_id,
)

RequirementLevelV1 = Literal["REQUIRED", "SUPPORTING"]
CompletionRoleV1 = Literal["CORE_DELIVERABLE", "REQUIRED_AUXILIARY", "SUPPORTING"]
EvaluationModeV1 = Literal["DETERMINISTIC", "EVIDENCE_GROUNDED_SEMANTIC"]

EVALUATOR_PROFILE_DETERMINISTIC = "criterion-evaluator-registry-v1"
EVALUATOR_PROFILE_EVIDENCE_GROUNDED = "evidence-grounded-semantic-v1"

_ROLE_TO_LEVEL: dict[CompletionRoleV1, RequirementLevelV1] = {
    "CORE_DELIVERABLE": "REQUIRED",
    "REQUIRED_AUXILIARY": "REQUIRED",
    "SUPPORTING": "SUPPORTING",
}


class ArtifactContractPredicateV1(StrictContractV1):
    kind: Literal["ARTIFACT_CONTRACT"]
    producer_plan_item_id: UUID
    output_schema_key: StableKey


class EvidenceSupportPredicateV1(StrictContractV1):
    kind: Literal["EVIDENCE_SUPPORT"]
    producer_plan_item_id: UUID
    min_citations: int = Field(ge=1, le=16)


class StateFactPredicateV1(StrictContractV1):
    kind: Literal["STATE_FACT"]
    subject_plan_item_id: UUID
    expected_state: Literal["COMPLETED"]


class UserFactPredicateV1(StrictContractV1):
    """预留谓词：v1编译器不产生；保留statement占位契约。"""

    kind: Literal["USER_FACT"]
    statement: Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class ConstraintPredicateV1(StrictContractV1):
    kind: Literal["CONSTRAINT"]
    constraint_ref: ConstraintRefV1


CriterionPredicateV1 = Annotated[
    ArtifactContractPredicateV1
    | EvidenceSupportPredicateV1
    | StateFactPredicateV1
    | UserFactPredicateV1
    | ConstraintPredicateV1,
    Field(discriminator="kind"),
]
CRITERION_PREDICATE_ADAPTER: TypeAdapter[CriterionPredicateV1] = TypeAdapter(
    CriterionPredicateV1
)


class CompiledCriterionV1(StrictContractV1):
    criterion_id: UUID
    statement: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    source_kind: CriterionSourceKindV1
    source_refs: tuple[StableKey, ...] = Field(min_length=1, max_length=8)
    requirement_level: RequirementLevelV1
    completion_role: CompletionRoleV1
    protected: bool
    evaluation_mode: EvaluationModeV1
    predicate: CriterionPredicateV1
    applicability: tuple[StableKey, ...] | None = None
    evaluator_profile: StableKey
    digest: Sha256Hex

    @model_validator(mode="after")
    def validate_derivation(self) -> CompiledCriterionV1:
        if self.requirement_level != _ROLE_TO_LEVEL[self.completion_role]:
            raise ValueError("REQUIREMENT_ROLE_INCONSISTENT")
        if self.source_kind in (
            CriterionSourceKindV1.USER,
            CriterionSourceKindV1.OUTPUT_CONTRACT,
            CriterionSourceKindV1.POLICY,
        ) and not self.protected:
            raise ValueError("AUTHORITATIVE_SOURCE_MUST_BE_PROTECTED")
        expected_mode = _evaluation_mode(self.predicate.kind)
        if self.evaluation_mode != expected_mode:
            raise ValueError("EVALUATION_MODE_INCONSISTENT")
        expected_profile = (
            EVALUATOR_PROFILE_EVIDENCE_GROUNDED
            if expected_mode == "EVIDENCE_GROUNDED_SEMANTIC"
            else EVALUATOR_PROFILE_DETERMINISTIC
        )
        if self.evaluator_profile != expected_profile:
            raise ValueError("EVALUATOR_PROFILE_INCONSISTENT")
        return self


def _evaluation_mode(predicate_kind: str) -> EvaluationModeV1:
    return "EVIDENCE_GROUNDED_SEMANTIC" if predicate_kind == "EVIDENCE_SUPPORT" else "DETERMINISTIC"


def _resolve_item(local_id: str, plan_item_ids: Mapping[str, UUID]) -> UUID:
    resolved = plan_item_ids.get(local_id)
    if resolved is None:
        raise ValueError("CRITERION_PRODUCER_ITEM_UNRESOLVED")
    return resolved


def _compile_predicate(
    draft: CriterionDraftV1, plan_item_ids: Mapping[str, UUID]
) -> CriterionPredicateV1:
    target = draft.target
    if isinstance(target, ArtifactCriterionTargetV1):
        return ArtifactContractPredicateV1(
            kind="ARTIFACT_CONTRACT",
            producer_plan_item_id=_resolve_item(target.producer_item_local_id, plan_item_ids),
            output_schema_key=target.output_schema_key,
        )
    if isinstance(target, CitationCriterionTargetV1):
        return EvidenceSupportPredicateV1(
            kind="EVIDENCE_SUPPORT",
            producer_plan_item_id=_resolve_item(target.producer_item_local_id, plan_item_ids),
            min_citations=target.min_citations,
        )
    if isinstance(target, StateCriterionTargetV1):
        return StateFactPredicateV1(
            kind="STATE_FACT",
            subject_plan_item_id=_resolve_item(target.subject_item_local_id, plan_item_ids),
            expected_state=target.expected_state,
        )
    if isinstance(target, UserCriterionTargetV1):
        return ConstraintPredicateV1(kind="CONSTRAINT", constraint_ref=target.constraint_ref)
    raise ValueError("UNKNOWN_CRITERION_TARGET")


def _completion_role(draft: CriterionDraftV1) -> CompletionRoleV1:
    return "CORE_DELIVERABLE" if isinstance(draft.target, ArtifactCriterionTargetV1) else (
        "REQUIRED_AUXILIARY"
    )


def _protected(source_kind: CriterionSourceKindV1, completion_role: CompletionRoleV1) -> bool:
    return (
        source_kind
        in (
            CriterionSourceKindV1.USER,
            CriterionSourceKindV1.OUTPUT_CONTRACT,
            CriterionSourceKindV1.POLICY,
        )
        or completion_role == "CORE_DELIVERABLE"
    )


def derive_criterion_source(
    draft: CriterionDraftV1,
) -> tuple[CriterionSourceKindV1, tuple[StableKey, ...]]:
    """从Draft确定性推导来源身份；OUTPUT_CONTRACT/POLICY需调用方显式提供。"""

    if isinstance(draft.target, UserCriterionTargetV1):
        return CriterionSourceKindV1.USER, (
            str(draft.target.constraint_ref.constraint_id).lower(),
        )
    if draft.goal_criterion_local_id is not None:
        return CriterionSourceKindV1.GOAL, (draft.goal_criterion_local_id,)
    return CriterionSourceKindV1.PLAN, (draft.local_id,)


def compile_criterion(
    *,
    task_id: UUID,
    plan_version: int,
    draft: CriterionDraftV1,
    source_kind: CriterionSourceKindV1,
    source_refs: tuple[StableKey, ...],
    plan_item_ids: Mapping[str, UUID],
) -> CompiledCriterionV1:
    """零LLM把`CriterionDraftV1`编译为`CompiledCriterionV1`。"""

    criterion_id = compiled_criterion_id(task_id, plan_version, source_kind, source_refs[0])
    predicate = _compile_predicate(draft, plan_item_ids)
    completion_role = _completion_role(draft)
    evaluation_mode = _evaluation_mode(predicate.kind)
    compiled = CompiledCriterionV1(
        criterion_id=criterion_id,
        statement=draft.statement,
        source_kind=source_kind,
        source_refs=source_refs,
        requirement_level=_ROLE_TO_LEVEL[completion_role],
        completion_role=completion_role,
        protected=_protected(source_kind, completion_role),
        evaluation_mode=evaluation_mode,
        predicate=predicate,
        applicability=None,
        evaluator_profile=(
            EVALUATOR_PROFILE_EVIDENCE_GROUNDED
            if evaluation_mode == "EVIDENCE_GROUNDED_SEMANTIC"
            else EVALUATOR_PROFILE_DETERMINISTIC
        ),
        digest="0" * 64,
    )
    digest = canonical_json_sha256(compiled.model_dump(mode="json", exclude={"digest"}))
    return compiled.model_copy(update={"digest": digest})


__all__ = [
    "ArtifactContractPredicateV1",
    "CitationCriterionTargetV1",
    "CompiledCriterionV1",
    "CompletionRoleV1",
    "ConstraintPredicateV1",
    "CriterionPredicateV1",
    "EvaluationModeV1",
    "EvidenceSupportPredicateV1",
    "RequirementLevelV1",
    "StateFactPredicateV1",
    "UserFactPredicateV1",
    "compile_criterion",
    "derive_criterion_source",
]
