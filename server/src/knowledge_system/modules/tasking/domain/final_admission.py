"""AGT-08 §5.33/§5.36：Final 支持目录投影 + Claim/Code/Artifact 准入 + 输出安全校验。

零 LLM、零 I/O、不接真实模型/PG/Catalog。本模块实现 §5.33 已冻结的 Claim 支持、Code 支持与
Result Artifact 准入三类纯领域规则，及支撑它们的 `FinalSupportCatalogEntryV1` 目录投影，外加
§5.36 `FinalOutputSecurityValidatorV1` 的文本/URL/Secret 注入检测。

`CitationResolverV1`（六种 Source Domain / 七种 Locator → c_001..c_200）依赖 Source Domain 与
Locator 分类枚举，留待后续子批次。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Literal

from pydantic import StringConstraints, model_validator

from .core_types import Sha256Hex, StableKey, StrictContractV1, VersionedRefV1
from .final_candidate import (
    FinalArtifactLinkBlockV1,
    FinalClaimDraftV1,
    FinalCodeBlockV1,
    FinalSynthesisCandidateV1,
)

FinalClaimVerdictV1 = Literal["SUPPORTED", "PARTIALLY_SUPPORTED", "UNSUPPORTED", "CONFLICTING"]
FinalSupportCatalogKindV1 = Literal[
    "VERIFIED_CLAIM",
    "EVIDENCE",
    "ADOPTED_ARTIFACT",
    "TASK_FACT",
    "USER_FACT",
    "CONFLICT",
    "GAP",
    "CODE_SNIPPET",
]
FinalResultArtifactKindV1 = Literal["REPORT", "TABLE", "FILE", "IMAGE", "DATASET", "OTHER"]

ClaimSupportVerdictV1 = Literal[
    "SUPPORTED",
    "MISSING_SUPPORTING_CITATION",
    "CANONICAL_TEXT_MISMATCH",
    "UNSUPPORTED_FACT_USAGE",
    "CONFLICTING_USED_AS_FACT",
    "UNANCHORED_NON_FACTUAL_CLAIM",
]
CodeSupportVerdictV1 = Literal["SUPPORTED", "UNVERIFIED_CODE"]

FinalSecurityViolationV1 = Literal[
    "HTML_TAG",
    "SCRIPT",
    "DANGEROUS_CONTROL_CHAR",
    "RESERVED_MARKER",
    "PATH_TRAVERSAL",
    "PROTOCOL_INJECTION",
    "SECRET",
    "CREDENTIALED_URL",
]


class FinalSupportCatalogEntryV1(StrictContractV1):
    """Context Builder 投影出的 Final 支持目录项（§5.33）。"""

    catalog_id: StableKey
    kind: FinalSupportCatalogKindV1
    source_ref: VersionedRefV1
    canonical_text: Annotated[str, StringConstraints(min_length=1, max_length=8192)] | None = None
    verdict: FinalClaimVerdictV1 | None = None
    artifact_kind: FinalResultArtifactKindV1 | None = None
    evidence_ref: VersionedRefV1 | None = None
    digest: Sha256Hex = "0" * 64

    @model_validator(mode="after")
    def validate_kind_specific(self) -> FinalSupportCatalogEntryV1:
        if self.verdict is not None and self.kind != "VERIFIED_CLAIM":
            raise ValueError("CATALOG_VERDICT_ONLY_FOR_VERIFIED_CLAIM")
        if self.kind == "VERIFIED_CLAIM" and (self.verdict is None or self.canonical_text is None):
            raise ValueError("VERIFIED_CLAIM_REQUIRES_VERDICT_AND_TEXT")
        if self.artifact_kind is not None and self.kind != "ADOPTED_ARTIFACT":
            raise ValueError("CATALOG_ARTIFACT_KIND_ONLY_FOR_ADOPTED_ARTIFACT")
        if self.kind == "ADOPTED_ARTIFACT" and self.artifact_kind is None:
            raise ValueError("ADOPTED_ARTIFACT_REQUIRES_ARTIFACT_KIND")
        if self.kind == "CODE_SNIPPET" and self.canonical_text is None:
            raise ValueError("CODE_SNIPPET_REQUIRES_TEXT")
        return self


_FACT_KINDS = frozenset({"SOURCE_FACT", "DERIVED_CONCLUSION"})
_BAD_VERDICTS = frozenset({"PARTIALLY_SUPPORTED", "UNSUPPORTED"})


def normalize_claim_text(text: str) -> str:
    """仅 NFC + 换行归一；数字/日期/实体/单位/否定/条件漂移均会改变字符串而被精确比较捕获。"""

    return unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")


def claim_support_verdict(
    claim: FinalClaimDraftV1, catalog: tuple[FinalSupportCatalogEntryV1, ...]
) -> ClaimSupportVerdictV1:
    """零 LLM Claim 支持判定（§5.33）。"""

    by_id = {entry.catalog_id: entry for entry in catalog}
    if len(by_id) != len(catalog):
        raise ValueError("CATALOG_DUPLICATE_ID")
    for catalog_id in claim.support_catalog_ids:
        if catalog_id not in by_id:
            raise ValueError("CLAIM_SUPPORT_REF_MISSING")

    referenced = [by_id[catalog_id] for catalog_id in claim.support_catalog_ids]
    verified = [entry for entry in referenced if entry.kind == "VERIFIED_CLAIM"]
    supported = [entry for entry in verified if entry.verdict == "SUPPORTED"]
    conflicting = [entry for entry in verified if entry.verdict == "CONFLICTING"]
    bad = [entry for entry in verified if entry.verdict in _BAD_VERDICTS]

    if claim.claim_kind in _FACT_KINDS:
        if conflicting:
            return "CONFLICTING_USED_AS_FACT"
        if not supported:
            if bad:
                return "UNSUPPORTED_FACT_USAGE"
            return "MISSING_SUPPORTING_CITATION"
        for entry in supported:
            assert entry.canonical_text is not None  # VERIFIED_CLAIM 校验器保证
            if normalize_claim_text(entry.canonical_text) == normalize_claim_text(claim.text):
                return "SUPPORTED"
        return "CANONICAL_TEXT_MISMATCH"

    # RECOMMENDATION / LIMITATION / NEXT_STEP 必须锚定 Criterion/Gap/Conflict/Task Fact
    anchored = (
        len(claim.criterion_ids) > 0
        or len(claim.conflict_set_ids) > 0
        or any(entry.kind in ("TASK_FACT", "USER_FACT") for entry in referenced)
    )
    return "SUPPORTED" if anchored else "UNANCHORED_NON_FACTUAL_CLAIM"


def code_block_support_verdict(
    block: FinalCodeBlockV1, catalog: tuple[FinalSupportCatalogEntryV1, ...]
) -> CodeSupportVerdictV1:
    """Code 必须与目录中 CODE_SNIPPET 内容摘要一致，不得现场生成未核验代码（§5.33）。"""

    by_id = {entry.catalog_id: entry for entry in catalog}
    if len(by_id) != len(catalog):
        raise ValueError("CATALOG_DUPLICATE_ID")
    for catalog_id in block.support_catalog_ids:
        if catalog_id not in by_id:
            raise ValueError("CODE_SUPPORT_REF_MISSING")
    for catalog_id in block.support_catalog_ids:
        entry = by_id[catalog_id]
        if entry.kind == "CODE_SNIPPET" and entry.canonical_text == block.code:
            return "SUPPORTED"
    return "UNVERIFIED_CODE"


def _artifact_link_ids(candidate: FinalSynthesisCandidateV1) -> tuple[StableKey, ...]:
    ids: list[StableKey] = []
    for section in candidate.sections:
        for block in section.blocks:
            if isinstance(block, FinalArtifactLinkBlockV1):
                ids.append(block.catalog_id)
    return tuple(ids)


def select_result_artifacts(
    candidate: FinalSynthesisCandidateV1, catalog: tuple[FinalSupportCatalogEntryV1, ...]
) -> tuple[StableKey, ...]:
    """Result Artifact 准入：目录内、ADOPTED、Artifact Link 与选择集合双向完备（§5.33）。"""

    by_id = {entry.catalog_id: entry for entry in catalog}
    if len(by_id) != len(catalog):
        raise ValueError("CATALOG_DUPLICATE_ID")

    selected = candidate.selected_result_artifact_catalog_ids
    if len(set(selected)) != len(selected):
        raise ValueError("SELECTED_ARTIFACT_DUPLICATE")

    for catalog_id in selected:
        entry = by_id.get(catalog_id)
        if entry is None:
            raise ValueError("SELECTED_ARTIFACT_NOT_IN_CATALOG")
        if entry.kind != "ADOPTED_ARTIFACT":
            raise ValueError("SELECTED_ARTIFACT_NOT_ADOPTED")

    link_ids = _artifact_link_ids(candidate)
    selected_set = set(selected)
    link_set = set(link_ids)
    if not link_set <= selected_set:
        raise ValueError("ARTIFACT_LINK_NOT_SELECTED")
    if not selected_set <= link_set:
        raise ValueError("SELECTED_ARTIFACT_NOT_LINKED")

    return selected


# --- §5.36 FinalOutputSecurityValidatorV1（文本/URL/Secret 注入检测） ---

_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>|<!")
_SCRIPT_RE = re.compile(r"<script|</script|javascript:|vbscript:|on\w+\s*=", re.IGNORECASE)
_PROTOCOL_RE = re.compile(r"\b(data|file|javascript|vbscript):", re.IGNORECASE)
_RESERVED_MARKER_RE = re.compile(r"\[c:")
_PATH_RE = re.compile(r"\.\./|\.\.\\|\x00")
_CREDENTIALED_URL_RE = re.compile(r"://[^/@\s]+:[^/@\s]+@")
_SECRET_RE = re.compile(
    r"-----BEGIN"
    r"|sk-[A-Za-z0-9]{8,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|gh[pous]_[A-Za-z0-9]{8,}"
    r"|github_pat_[A-Za-z0-9_]{8,}"
    r"|Bearer\s+[A-Za-z0-9._-]{8,}"
    r"|Authorization\s*:",
    re.IGNORECASE,
)
_SECRET_KEYWORD_RE = re.compile(
    r"(api[-_]?key|apikey|password|passwd|pwd|access[-_]?token|secret|token)\s*[:=]",
    re.IGNORECASE,
)

_ALLOWED_CONTROL = frozenset("\n\r\t")


def _is_dangerous_control_char(ch: str) -> bool:
    return unicodedata.category(ch) == "Cc" and ch not in _ALLOWED_CONTROL


def scan_output_security(*texts: str) -> tuple[FinalSecurityViolationV1, ...]:
    """对任意用户可见文本做安全扫描；返回去重后的违规集合（空 = 安全，失败关闭）。"""

    violations: set[FinalSecurityViolationV1] = set()
    for text in texts:
        if not text:
            continue
        if _RESERVED_MARKER_RE.search(text):
            violations.add("RESERVED_MARKER")
        if _SCRIPT_RE.search(text):
            violations.add("SCRIPT")
        elif _HTML_TAG_RE.search(text):
            violations.add("HTML_TAG")
        if _PROTOCOL_RE.search(text):
            violations.add("PROTOCOL_INJECTION")
        if _PATH_RE.search(text):
            violations.add("PATH_TRAVERSAL")
        if _CREDENTIALED_URL_RE.search(text):
            violations.add("CREDENTIALED_URL")
        if _SECRET_RE.search(text) or _SECRET_KEYWORD_RE.search(text):
            violations.add("SECRET")
        if any(_is_dangerous_control_char(ch) for ch in text):
            violations.add("DANGEROUS_CONTROL_CHAR")
    return tuple(sorted(violations))


def output_is_safe(*texts: str) -> bool:
    return not scan_output_security(*texts)


def scan_candidate_security(
    candidate: FinalSynthesisCandidateV1,
) -> tuple[FinalSecurityViolationV1, ...]:
    """收集候选全部用户可见文本字段的安全违规（§5.36）。"""

    texts = [candidate.title]
    for claim in candidate.claims:
        texts.append(claim.text)
    for section in candidate.sections:
        texts.append(section.heading)
        for block in section.blocks:
            if isinstance(block, FinalArtifactLinkBlockV1):
                texts.append(block.display_name)
            elif isinstance(block, FinalCodeBlockV1):
                texts.append(block.code)
    return scan_output_security(*texts)


__all__ = [
    "ClaimSupportVerdictV1",
    "CodeSupportVerdictV1",
    "FinalClaimVerdictV1",
    "FinalResultArtifactKindV1",
    "FinalSecurityViolationV1",
    "FinalSupportCatalogEntryV1",
    "FinalSupportCatalogKindV1",
    "claim_support_verdict",
    "code_block_support_verdict",
    "normalize_claim_text",
    "output_is_safe",
    "scan_candidate_security",
    "scan_output_security",
    "select_result_artifacts",
]
