"""AGT-08 §5.33：零 LLM `FinalCandidateValidatorV1` 编排。

固定按 Schema/128KiB、Snapshot绑定、局部图引用、UTF-8/NFC/控制字符/HTML/保留Marker、Claim支持、
语义Criterion、Citation、Artifact/Output Contract 顺序执行，输出不可变
`FinalCandidateValidationV1`：
`ACCEPT/REPAIRABLE_INVALID/SNAPSHOT_STALE/UNSAFE/UNRECOVERABLE_INVALID`。

本切片只编排已落地确定性校验器（`final_candidate`/`final_gate`/`final_admission`/`final_citation`/
`final_evaluation`/`final_synthesis`），不改写模型语义；Citation 编号、转义与元数据解析属确定性
装配。
Renderer 预演与最终 Policy/版本/高水位需 §5.34 Renderer 与 §5.35 运行编排接线，本切片不落地。

分类约定（顺序执行、首败即返）：
- 结构/契约不可修复（Size、悬空引用、目录缺失/重复、语义覆盖不齐、引用源缺失）→
  `UNRECOVERABLE_INVALID`；
- 语义可定向修复（Claim 文本漂移、缺支持、Code 未核验、需引用、Artifact 双向不完备、契约引用缺/禁）→
  `REPAIRABLE_INVALID`（携带有界 repair_paths）；
- Snapshot 绑定不符或授权已撤 → `SNAPSHOT_STALE`；安全扫描命中 → `UNSAFE`（携
  security_violations）。
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from .core_types import Sha256Hex, StableKey, StrictContractV1, canonical_json_sha256
from .final_admission import (
    FinalSecurityViolationV1,
    FinalSupportCatalogEntryV1,
    claim_support_verdict,
    code_block_support_verdict,
    scan_candidate_security,
    select_result_artifacts,
)
from .final_candidate import (
    FinalClaimDraftV1,
    FinalCodeBlockV1,
    FinalListBlockV1,
    FinalParagraphBlockV1,
    FinalSynthesisCandidateV1,
    FinalTableBlockV1,
    validate_candidate_size,
)
from .final_citation import CitationSourceV1, resolve_citations
from .final_evaluation import CriterionEvaluationStateV1, evaluate_all_criteria
from .final_synthesis import FinalSynthesisSnapshotV1, final_synthesis_snapshot_id

FinalCandidateValidationVerdictV1 = Literal[
    "ACCEPT", "REPAIRABLE_INVALID", "SNAPSHOT_STALE", "UNSAFE", "UNRECOVERABLE_INVALID"
]

_REPAIRABLE_ARTIFACT_CODES = frozenset(
    {"ARTIFACT_LINK_NOT_SELECTED", "SELECTED_ARTIFACT_NOT_LINKED"}
)


class FinalCandidateValidationV1(StrictContractV1):
    """不可变候选校验结果：五态判定 + 稳定原因码 + 有界修复路径 + 安全违规。"""

    contract_version: Literal["final_candidate_validation_v1"]
    snapshot_id: UUID
    candidate_digest: Sha256Hex
    verdict: FinalCandidateValidationVerdictV1
    reason_codes: tuple[StableKey, ...] = ()
    repair_paths: tuple[StableKey, ...] = ()
    security_violations: tuple[FinalSecurityViolationV1, ...] = ()
    validation_digest: Sha256Hex = "0" * 64


def _build_validation(
    *,
    snapshot_id: UUID,
    candidate_digest: Sha256Hex,
    verdict: FinalCandidateValidationVerdictV1,
    reason_codes: tuple[str, ...] = (),
    repair_paths: tuple[str, ...] = (),
    security_violations: tuple[FinalSecurityViolationV1, ...] = (),
) -> FinalCandidateValidationV1:
    record = FinalCandidateValidationV1(
        contract_version="final_candidate_validation_v1",
        snapshot_id=snapshot_id,
        candidate_digest=candidate_digest,
        verdict=verdict,
        reason_codes=reason_codes,
        repair_paths=repair_paths,
        security_violations=security_violations,
        validation_digest="0" * 64,
    )
    digest = canonical_json_sha256(record.model_dump(mode="json", exclude={"validation_digest"}))
    return record.model_copy(update={"validation_digest": digest})


def _block_claim_ids(block: object) -> tuple[str, ...]:
    """提取内容块引用的 Claim 本地 ID（判别联合在运行时已收窄为具体类型）。"""

    if isinstance(block, FinalParagraphBlockV1):
        return block.claim_local_ids
    if isinstance(block, FinalListBlockV1):
        return tuple(local_id for item in block.items for local_id in item)
    if isinstance(block, FinalTableBlockV1):
        return tuple(local_id for row in block.rows for cell in row for local_id in cell)
    return ()


def _check_local_graph(
    candidate: FinalSynthesisCandidateV1,
    snapshot: FinalSynthesisSnapshotV1,
    catalog: tuple[FinalSupportCatalogEntryV1, ...],
) -> str | None:
    """局部图引用完整性：所有本地 ID/UUID 引用必须命中对应权威集合。"""

    claim_ids = {claim.local_claim_id for claim in candidate.claims}
    criterion_ids = {criterion.criterion_id for criterion in snapshot.compiled_criteria}
    conflict_ids = set(snapshot.conflict_ids)
    catalog_ids = {entry.catalog_id for entry in catalog}

    for section in candidate.sections:
        for block in section.blocks:
            for local_id in _block_claim_ids(block):
                if local_id not in claim_ids:
                    return "CANDIDATE_CLAIM_REF_MISSING"

    for claim in candidate.claims:
        for criterion_id in claim.criterion_ids:
            if criterion_id not in criterion_ids:
                return "CANDIDATE_CRITERION_REF_MISSING"
        for conflict_id in claim.conflict_set_ids:
            if conflict_id not in conflict_ids:
                return "CANDIDATE_CONFLICT_REF_MISSING"

    for assessment in candidate.semantic_criterion_assessments:
        if assessment.criterion_id not in criterion_ids:
            return "ASSESSMENT_CRITERION_REF_MISSING"
        for local_id in assessment.claim_local_ids:
            if local_id not in claim_ids:
                return "ASSESSMENT_CLAIM_REF_MISSING"
        for catalog_id in assessment.supporting_catalog_ids:
            if catalog_id not in catalog_ids:
                return "ASSESSMENT_CATALOG_REF_MISSING"

    for gap in candidate.gap_reports:
        for criterion_id in gap.affected_criterion_ids:
            if criterion_id not in criterion_ids:
                return "GAP_CRITERION_REF_MISSING"
        for source_gap_id in gap.source_gap_ids:
            if source_gap_id not in catalog_ids:
                return "GAP_SOURCE_REF_MISSING"

    return None


def _claim_repair_path(claim: FinalClaimDraftV1, verdict: str) -> str:
    if verdict == "CANONICAL_TEXT_MISMATCH":
        return f"claims/{claim.local_claim_id}/text"
    if verdict == "UNANCHORED_NON_FACTUAL_CLAIM":
        return f"claims/{claim.local_claim_id}/criterion_ids"
    return f"claims/{claim.local_claim_id}/support_catalog_ids"


def _first_claim_support_failure(
    candidate: FinalSynthesisCandidateV1, catalog: tuple[FinalSupportCatalogEntryV1, ...]
) -> tuple[str, str] | None:
    for claim in candidate.claims:
        verdict = claim_support_verdict(claim, catalog)
        if verdict != "SUPPORTED":
            return (verdict, _claim_repair_path(claim, verdict))
    return None


def _first_code_support_failure(
    candidate: FinalSynthesisCandidateV1, catalog: tuple[FinalSupportCatalogEntryV1, ...]
) -> tuple[str, str] | None:
    for section in candidate.sections:
        for index, block in enumerate(section.blocks):
            if isinstance(block, FinalCodeBlockV1):
                verdict = code_block_support_verdict(block, catalog)
                if verdict != "SUPPORTED":
                    return (verdict, f"sections/{section.local_section_id}/blocks/{index}/code")
    return None


def _check_output_contract_citations(
    snapshot: FinalSynthesisSnapshotV1, citation_count: int
) -> str | None:
    contract = snapshot.output_contract
    if contract.citations == "REQUIRED" and citation_count == 0:
        return "OUTPUT_CONTRACT_CITATIONS_REQUIRED"
    if contract.citations == "FORBIDDEN" and citation_count > 0:
        return "OUTPUT_CONTRACT_CITATIONS_FORBIDDEN"
    return None


def validate_final_candidate(
    *,
    snapshot: FinalSynthesisSnapshotV1,
    candidate: FinalSynthesisCandidateV1,
    catalog: tuple[FinalSupportCatalogEntryV1, ...],
    citation_sources: tuple[CitationSourceV1, ...],
    state: CriterionEvaluationStateV1,
) -> FinalCandidateValidationV1:
    """按 §5.33 固定顺序编排确定性校验，输出五态 `FinalCandidateValidationV1`。"""

    snapshot_id = final_synthesis_snapshot_id(
        task_id=snapshot.task_id,
        runtime_decision_id=snapshot.runtime_decision_id,
        snapshot_digest=snapshot.snapshot_digest,
        builder_profile=snapshot.builder_profile,
    )
    candidate_digest = canonical_json_sha256(candidate.model_dump(mode="json"))

    # 1. Schema/128KiB
    try:
        validate_candidate_size(candidate)
    except ValueError as err:
        return _build_validation(
            snapshot_id=snapshot_id,
            candidate_digest=candidate_digest,
            verdict="UNRECOVERABLE_INVALID",
            reason_codes=(str(err),),
        )

    # 2. Snapshot 绑定
    if (
        candidate.snapshot_id != snapshot_id
        or candidate.snapshot_digest != snapshot.snapshot_digest
    ):
        return _build_validation(
            snapshot_id=snapshot_id,
            candidate_digest=candidate_digest,
            verdict="SNAPSHOT_STALE",
            reason_codes=("FINAL_SYNTHESIS_SNAPSHOT_STALE",),
        )

    # 2b. 授权已撤 → stale
    if snapshot.authorization_revoked:
        return _build_validation(
            snapshot_id=snapshot_id,
            candidate_digest=candidate_digest,
            verdict="SNAPSHOT_STALE",
            reason_codes=("AUTHORIZATION_REVOKED",),
        )

    # 3. 局部图引用
    graph_error = _check_local_graph(candidate, snapshot, catalog)
    if graph_error is not None:
        return _build_validation(
            snapshot_id=snapshot_id,
            candidate_digest=candidate_digest,
            verdict="UNRECOVERABLE_INVALID",
            reason_codes=(graph_error,),
        )

    # 4. 安全扫描
    violations = scan_candidate_security(candidate)
    if violations:
        return _build_validation(
            snapshot_id=snapshot_id,
            candidate_digest=candidate_digest,
            verdict="UNSAFE",
            reason_codes=("FINAL_MODEL_OUTPUT_UNSAFE",),
            security_violations=violations,
        )

    # 5. Claim/Code 支持
    try:
        claim_failure = _first_claim_support_failure(candidate, catalog)
    except ValueError as err:
        return _build_validation(
            snapshot_id=snapshot_id,
            candidate_digest=candidate_digest,
            verdict="UNRECOVERABLE_INVALID",
            reason_codes=(str(err),),
        )
    if claim_failure is not None:
        return _build_validation(
            snapshot_id=snapshot_id,
            candidate_digest=candidate_digest,
            verdict="REPAIRABLE_INVALID",
            reason_codes=(claim_failure[0],),
            repair_paths=(claim_failure[1],),
        )
    try:
        code_failure = _first_code_support_failure(candidate, catalog)
    except ValueError as err:
        return _build_validation(
            snapshot_id=snapshot_id,
            candidate_digest=candidate_digest,
            verdict="UNRECOVERABLE_INVALID",
            reason_codes=(str(err),),
        )
    if code_failure is not None:
        return _build_validation(
            snapshot_id=snapshot_id,
            candidate_digest=candidate_digest,
            verdict="REPAIRABLE_INVALID",
            reason_codes=(code_failure[0],),
            repair_paths=(code_failure[1],),
        )

    # 6. 语义 Criterion 覆盖 + 确定性求值
    try:
        evaluate_all_criteria(
            compiled_criteria=snapshot.compiled_criteria,
            state=state,
            semantic_assessments=candidate.semantic_criterion_assessments,
        )
    except ValueError as err:
        return _build_validation(
            snapshot_id=snapshot_id,
            candidate_digest=candidate_digest,
            verdict="UNRECOVERABLE_INVALID",
            reason_codes=(str(err),),
        )

    # 7. Citation
    try:
        citations = resolve_citations(candidate.claims, catalog, citation_sources)
    except ValueError as err:
        code = str(err)
        if code == "CLAIM_REQUIRES_CITATION":
            return _build_validation(
                snapshot_id=snapshot_id,
                candidate_digest=candidate_digest,
                verdict="REPAIRABLE_INVALID",
                reason_codes=(code,),
                repair_paths=("claims",),
            )
        return _build_validation(
            snapshot_id=snapshot_id,
            candidate_digest=candidate_digest,
            verdict="UNRECOVERABLE_INVALID",
            reason_codes=(code,),
        )

    # 8. Artifact 准入
    try:
        select_result_artifacts(candidate, catalog)
    except ValueError as err:
        code = str(err)
        if code in _REPAIRABLE_ARTIFACT_CODES:
            return _build_validation(
                snapshot_id=snapshot_id,
                candidate_digest=candidate_digest,
                verdict="REPAIRABLE_INVALID",
                reason_codes=(code,),
                repair_paths=("selected_result_artifact_catalog_ids",),
            )
        return _build_validation(
            snapshot_id=snapshot_id,
            candidate_digest=candidate_digest,
            verdict="UNRECOVERABLE_INVALID",
            reason_codes=(code,),
        )

    # 9. Output Contract 引用要求
    contract_error = _check_output_contract_citations(snapshot, len(citations))
    if contract_error is not None:
        return _build_validation(
            snapshot_id=snapshot_id,
            candidate_digest=candidate_digest,
            verdict="REPAIRABLE_INVALID",
            reason_codes=(contract_error,),
            repair_paths=("claims",),
        )

    # 10. Renderer 预演与最终 Policy/版本/高水位：§5.34/§5.35 接线后补。
    return _build_validation(
        snapshot_id=snapshot_id,
        candidate_digest=candidate_digest,
        verdict="ACCEPT",
    )


__all__ = [
    "FinalCandidateValidationV1",
    "FinalCandidateValidationVerdictV1",
    "validate_final_candidate",
]
