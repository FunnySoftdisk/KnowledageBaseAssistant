"""AGT-08 §5.33/§5.34：零 LLM `CitationResolverV1` 引用解析。

六种 Source Domain 与七种 Locator 已完善（见模块设计 §5.33）：外部侧按 G07/G13 冻结 Schema 取
`source_kind`（EXTERNAL_WEB/EXTERNAL_SCHOLARLY_RECORD/EXTERNAL_CODE）与 `locator_kind`
（WEB/DOI/ARXIV/GITHUB_CONTENT）；内部侧按知识目录/任务制品/任务·用户事实取
INTERNAL_KNOWLEDGE/TASK_ARTIFACT/TASK_FACT 与 KNOWLEDGE_ENTRY/ARTIFACT_REF/FACT_REF。

本模块只做确定性引用分配：按最终 Claim 首次出现顺序去重 Evidence 并分配 c_001..c_200，强制
每个需引用 Claim 至少一个 Citation、同一 Evidence 不重复、Domain/Locator 一致。Preview 由当前
授权 Evidence 生成（最多 512 字符）；模型不生成 Citation ID/Locator/Preview。
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import StringConstraints, model_validator

from .core_types import StrictContractV1
from .final_admission import FinalSupportCatalogEntryV1
from .final_candidate import FinalClaimDraftV1

CitationSourceDomainV1 = Literal[
    "EXTERNAL_WEB",
    "EXTERNAL_SCHOLARLY_RECORD",
    "EXTERNAL_CODE",
    "INTERNAL_KNOWLEDGE",
    "TASK_ARTIFACT",
    "TASK_FACT",
]
CitationLocatorKindV1 = Literal[
    "WEB",
    "DOI",
    "ARXIV",
    "GITHUB_CONTENT",
    "KNOWLEDGE_ENTRY",
    "ARTIFACT_REF",
    "FACT_REF",
]
CitationId = Annotated[str, StringConstraints(pattern=r"^c_\d{3}$")]

_DOMAIN_LOCATORS: dict[str, frozenset[str]] = {
    "EXTERNAL_WEB": frozenset({"WEB"}),
    "EXTERNAL_SCHOLARLY_RECORD": frozenset({"DOI", "ARXIV"}),
    "EXTERNAL_CODE": frozenset({"GITHUB_CONTENT"}),
    "INTERNAL_KNOWLEDGE": frozenset({"KNOWLEDGE_ENTRY"}),
    "TASK_ARTIFACT": frozenset({"ARTIFACT_REF"}),
    "TASK_FACT": frozenset({"FACT_REF"}),
}


class CitationSourceV1(StrictContractV1):
    """一个可引用 Evidence 的引用源投影（服务端从当前授权 Evidence 构造）。"""

    evidence_ref_id: UUID
    source_domain: CitationSourceDomainV1
    locator_kind: CitationLocatorKindV1
    locator_value: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    preview: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None

    @model_validator(mode="after")
    def validate_domain_locator(self) -> CitationSourceV1:
        if self.locator_kind not in _DOMAIN_LOCATORS[self.source_domain]:
            raise ValueError("CITATION_LOCATOR_DOMAIN_MISMATCH")
        return self


class FinalCitationV1(StrictContractV1):
    citation_id: CitationId
    evidence_ref_id: UUID
    source_domain: CitationSourceDomainV1
    locator_kind: CitationLocatorKindV1
    locator_value: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    preview: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None


_FACT_KINDS = frozenset({"SOURCE_FACT", "DERIVED_CONCLUSION"})


def _claim_evidence_ids(
    claim: FinalClaimDraftV1, by_id: dict[str, FinalSupportCatalogEntryV1]
) -> list[UUID]:
    ids: list[UUID] = []
    for catalog_id in claim.support_catalog_ids:
        entry = by_id.get(catalog_id)
        if entry is None:
            raise ValueError("CLAIM_SUPPORT_REF_MISSING")
        if (
            entry.kind == "VERIFIED_CLAIM"
            and entry.verdict == "SUPPORTED"
            and entry.evidence_ref is not None
            and entry.evidence_ref.ref_id not in ids
        ):
            ids.append(entry.evidence_ref.ref_id)
    return ids


def resolve_citations(
    claims: tuple[FinalClaimDraftV1, ...],
    catalog: tuple[FinalSupportCatalogEntryV1, ...],
    citation_sources: tuple[CitationSourceV1, ...],
) -> tuple[FinalCitationV1, ...]:
    """按最终 Claim 首次出现顺序去重 Evidence 并分配 c_001..c_200。"""

    by_id = {entry.catalog_id: entry for entry in catalog}
    if len(by_id) != len(catalog):
        raise ValueError("CATALOG_DUPLICATE_ID")
    src_by_evidence = {source.evidence_ref_id: source for source in citation_sources}
    if len(src_by_evidence) != len(citation_sources):
        raise ValueError("CITATION_SOURCE_DUPLICATE")

    citations: list[FinalCitationV1] = []
    seen: dict[UUID, str] = {}
    counter = 0

    for claim in claims:
        if claim.claim_kind not in _FACT_KINDS:
            continue
        evidence_ids = _claim_evidence_ids(claim, by_id)
        if not evidence_ids:
            raise ValueError("CLAIM_REQUIRES_CITATION")
        for evidence_id in evidence_ids:
            if evidence_id in seen:
                continue
            source = src_by_evidence.get(evidence_id)
            if source is None:
                raise ValueError("CITATION_SOURCE_MISSING")
            counter += 1
            if counter > 200:
                raise ValueError("CITATION_LIMIT_EXCEEDED")
            citation_id = f"c_{counter:03d}"
            seen[evidence_id] = citation_id
            citations.append(
                FinalCitationV1(
                    citation_id=citation_id,
                    evidence_ref_id=evidence_id,
                    source_domain=source.source_domain,
                    locator_kind=source.locator_kind,
                    locator_value=source.locator_value,
                    preview=source.preview,
                )
            )
    return tuple(citations)


def claim_citation_ids(
    claim: FinalClaimDraftV1,
    catalog: tuple[FinalSupportCatalogEntryV1, ...],
    citations: tuple[FinalCitationV1, ...],
) -> tuple[str, ...]:
    """claim 引用的 Evidence 对应的 Citation ID（按出现顺序，去重）。"""

    by_id = {entry.catalog_id: entry for entry in catalog}
    citation_by_evidence = {
        citation.evidence_ref_id: citation.citation_id for citation in citations
    }
    result: list[str] = []
    for evidence_id in _claim_evidence_ids(claim, by_id):
        citation_id = citation_by_evidence.get(evidence_id)
        if citation_id is not None and citation_id not in result:
            result.append(citation_id)
    return tuple(result)


__all__ = [
    "CitationId",
    "CitationLocatorKindV1",
    "CitationSourceDomainV1",
    "CitationSourceV1",
    "FinalCitationV1",
    "claim_citation_ids",
    "resolve_citations",
]
