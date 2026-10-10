"""AGT-08 §5.32：`final_synthesis_v1` 模型候选严格契约（零 LLM 校验对象）。

模型只生成候选；候选是封闭 Schema，`FinalCandidateValidatorV1` 在后续切片逐项校验。
本模块只定义候选、内容块判别联合与本地图引用关系，不执行 I/O、授权或状态判定。
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

import rfc8785
from pydantic import Field, StringConstraints, TypeAdapter, model_validator

from .core_types import Sha256Hex, StableKey, StrictContractV1
from .final_synthesis import FinalGapKindV1
from .goal_contracts import LocalId

CANDIDATE_MAX_BLOCKS = 64
CANDIDATE_MAX_BYTES = 131_072

SemanticCriterionAssessmentKindV1 = Literal[
    "SUPPORTED", "PARTIALLY_SUPPORTED", "UNSUPPORTED", "UNKNOWN"
]
FinalClaimKindV1 = Literal[
    "SOURCE_FACT", "DERIVED_CONCLUSION", "RECOMMENDATION", "LIMITATION", "NEXT_STEP"
]
GapNextStep = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class FinalParagraphBlockV1(StrictContractV1):
    block_kind: Literal["PARAGRAPH"]
    claim_local_ids: tuple[LocalId, ...] = Field(min_length=1, max_length=8)


class FinalListBlockV1(StrictContractV1):
    block_kind: Literal["BULLET_LIST"]
    ordered: bool
    items: tuple[tuple[LocalId, ...], ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_items(self) -> FinalListBlockV1:
        if any(len(item) < 1 or len(item) > 4 for item in self.items):
            raise ValueError("LIST_ITEM_CLAIM_COUNT_INVALID")
        return self


class FinalTableBlockV1(StrictContractV1):
    block_kind: Literal["TABLE"]
    column_count: int = Field(ge=1, le=12)
    rows: tuple[tuple[tuple[LocalId, ...], ...], ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_shape(self) -> FinalTableBlockV1:
        if any(len(row) != self.column_count for row in self.rows):
            raise ValueError("TABLE_COLUMN_COUNT_MISMATCH")
        if any(len(cell) > 4 for row in self.rows for cell in row):
            raise ValueError("TABLE_CELL_CLAIM_COUNT_INVALID")
        return self


class FinalCodeBlockV1(StrictContractV1):
    block_kind: Literal["CODE"]
    language: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    code: Annotated[str, StringConstraints(min_length=1, max_length=8192)]
    support_catalog_ids: tuple[StableKey, ...] = Field(max_length=16)


class FinalArtifactLinkBlockV1(StrictContractV1):
    block_kind: Literal["ARTIFACT_LINK"]
    catalog_id: StableKey
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=256)]


FinalContentBlockV1 = Annotated[
    FinalParagraphBlockV1
    | FinalListBlockV1
    | FinalTableBlockV1
    | FinalCodeBlockV1
    | FinalArtifactLinkBlockV1,
    Field(discriminator="block_kind"),
]
FINAL_CONTENT_BLOCK_ADAPTER: TypeAdapter[FinalContentBlockV1] = TypeAdapter(FinalContentBlockV1)


class SemanticCriterionAssessmentV1(StrictContractV1):
    criterion_id: UUID
    assessment: SemanticCriterionAssessmentKindV1
    supporting_catalog_ids: tuple[StableKey, ...] = Field(max_length=16)
    claim_local_ids: tuple[LocalId, ...] = Field(max_length=16)
    note: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class FinalGapReportV1(StrictContractV1):
    local_gap_id: LocalId
    gap_kind: FinalGapKindV1
    description: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    source_gap_ids: tuple[StableKey, ...] = Field(max_length=8)
    affected_criterion_ids: tuple[UUID, ...] = Field(max_length=12)
    next_steps: tuple[GapNextStep, ...] = Field(max_length=5)


class FinalClaimDraftV1(StrictContractV1):
    local_claim_id: LocalId
    claim_kind: FinalClaimKindV1
    text: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    support_catalog_ids: tuple[StableKey, ...] = Field(max_length=16)
    criterion_ids: tuple[UUID, ...] = Field(max_length=12)
    conflict_set_ids: tuple[UUID, ...] = Field(max_length=8)


class FinalSectionDraftV1(StrictContractV1):
    local_section_id: LocalId
    heading: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    blocks: tuple[FinalContentBlockV1, ...] = Field(min_length=1, max_length=16)


class FinalSynthesisCandidateV1(StrictContractV1):
    contract_version: Literal["final_synthesis_v1"]
    snapshot_id: UUID
    snapshot_digest: Sha256Hex
    title: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    claims: tuple[FinalClaimDraftV1, ...] = Field(max_length=200)
    sections: tuple[FinalSectionDraftV1, ...] = Field(max_length=32)
    semantic_criterion_assessments: tuple[SemanticCriterionAssessmentV1, ...] = Field(max_length=12)
    gap_reports: tuple[FinalGapReportV1, ...] = Field(max_length=12)
    selected_result_artifact_catalog_ids: tuple[StableKey, ...] = Field(max_length=50)

    @model_validator(mode="after")
    def validate_shape(self) -> FinalSynthesisCandidateV1:
        if not self.sections and not self.gap_reports:
            raise ValueError("CANDIDATE_SECTION_OR_GAP_REQUIRED")
        total_blocks = sum(len(section.blocks) for section in self.sections)
        if total_blocks > CANDIDATE_MAX_BLOCKS:
            raise ValueError("CANDIDATE_TOTAL_BLOCK_LIMIT")
        for label, values in (
            ("claim", [claim.local_claim_id for claim in self.claims]),
            ("section", [section.local_section_id for section in self.sections]),
            ("gap", [gap.local_gap_id for gap in self.gap_reports]),
            (
                "criterion_assessment",
                [assessment.criterion_id for assessment in self.semantic_criterion_assessments],
            ),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"CANDIDATE_DUPLICATE_{label.upper()}_ID")
        return self


def candidate_canonical_bytes(candidate: FinalSynthesisCandidateV1) -> int:
    return len(rfc8785.dumps(candidate.model_dump(mode="json")))


def validate_candidate_size(candidate: FinalSynthesisCandidateV1) -> None:
    """128 KiB 上限在 Validator 阶段强制；Schema 阶段只校验形状。"""

    if candidate_canonical_bytes(candidate) > CANDIDATE_MAX_BYTES:
        raise ValueError("CANDIDATE_SIZE_LIMIT_EXCEEDED")


__all__ = [
    "CANDIDATE_MAX_BLOCKS",
    "CANDIDATE_MAX_BYTES",
    "FINAL_CONTENT_BLOCK_ADAPTER",
    "FinalArtifactLinkBlockV1",
    "FinalClaimDraftV1",
    "FinalClaimKindV1",
    "FinalCodeBlockV1",
    "FinalContentBlockV1",
    "FinalGapReportV1",
    "FinalListBlockV1",
    "FinalParagraphBlockV1",
    "FinalSectionDraftV1",
    "FinalSynthesisCandidateV1",
    "FinalTableBlockV1",
    "SemanticCriterionAssessmentKindV1",
    "SemanticCriterionAssessmentV1",
    "candidate_canonical_bytes",
    "validate_candidate_size",
]
