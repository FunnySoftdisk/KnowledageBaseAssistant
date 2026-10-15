"""DEV-09：文档处理解析运行模型 + CLEAN 输入准入 + 解析规划（零 LLM/零 I/O）。

权威设计：模块05《文档处理模块详细设计》§3（运行模型）/§4.2（协调阶段）/§5（CLEAN 输入准入与
解析规划）。本文件只冻结：
- 协调阶段与运行状态的枚举，`ParseRunV1` 聚合根契约（§3）与其幂等键（§3）；
- `admit_clean_input`（§5 读 `artifact_preflight_v1` 证明：结论 CLEAN + Policy 适用于该来源 +
  libmagic/libarchive/qpdf/ClamAV 证据完整，否则不创建 ParseRun）；
- `plan_inspection`（§5 `DocumentParsePlanner.inspect` 只提取解析策略所需事实：页数/页尺寸/旋转/
  文本层/文本覆盖比例/预计像素量，不重复 Preflight 结论）。

不访问模型、网络、OCR、ORM 或 Temporal。解析器适配、`ParseCoordinator` 编排、Checkpoint 持久化与
真实 Preflight/工具授权证据读取均属后续切片。
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import Field

from knowledge_system.modules.tasking.domain.core_types import (
    ArtifactRefV1,
    Sha256Hex,
    ShortVersion,
    StrictContractV1,
    canonical_json_sha256,
)

from .document import QualityDecisionV1


class ParseStageV1(StrEnum):
    """§4.2 协调阶段（顺序固定）。每阶段结束写 Checkpoint，重试从最近完整阶段恢复。"""

    ADMIT_CLEAN_INPUT = "ADMIT_CLEAN_INPUT"
    PARSE_PLANNING = "PARSE_PLANNING"
    PRIMARY_PARSE = "PRIMARY_PARSE"
    ACADEMIC_ENRICH = "ACADEMIC_ENRICH"
    QUALITY_SCAN = "QUALITY_SCAN"
    REGION_FALLBACK = "REGION_FALLBACK"
    CANONICALIZE = "CANONICALIZE"
    SERIALIZE = "SERIALIZE"
    FINAL_VALIDATE = "FINAL_VALIDATE"


_STAGE_ORDER: tuple[ParseStageV1, ...] = tuple(ParseStageV1)


class ParseRunStatusV1(StrEnum):
    """§3/§11 运行生命周期状态。终态（PASSED/FAILED/CANCELLED/STALE）无出边。

    PASSED 表示运行走完全部阶段并产出 CanonicalDocument（质量结论另存于 quality_decision，
    REVIEW_REQUIRED/FAIL 是质量结论而非运行状态）；STALE 对应 §11「旧知识版本任务」。
    """

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    STALE = "STALE"


_LEGAL_STATUS_TRANSITIONS: dict[ParseRunStatusV1, frozenset[ParseRunStatusV1]] = {
    ParseRunStatusV1.CREATED: frozenset(
        {ParseRunStatusV1.RUNNING, ParseRunStatusV1.CANCELLED, ParseRunStatusV1.STALE}
    ),
    ParseRunStatusV1.RUNNING: frozenset(
        {
            ParseRunStatusV1.PASSED,
            ParseRunStatusV1.FAILED,
            ParseRunStatusV1.CANCELLED,
            ParseRunStatusV1.STALE,
        }
    ),
    ParseRunStatusV1.PASSED: frozenset(),
    ParseRunStatusV1.FAILED: frozenset(),
    ParseRunStatusV1.CANCELLED: frozenset(),
    ParseRunStatusV1.STALE: frozenset(),
}


class ParseRunV1(StrictContractV1):
    """§3 解析运行聚合根。`source_sha256` 即 §3 的 `input_sha256`（输入文件内容 SHA）。

    幂等键 = `source_sha256 + parser_bundle_version + parser_config_hash`（见
    `compute_parse_run_idempotency_key`）；已有 PASSED 结果复用 Artifact，已有执行中任务返回该
    ParseRun。ID 采用普通 UUID（与既有 memory/tasking 代码一致，不引入 UUIDValue 强类型）。
    """

    id: UUID
    knowledge_version_id: UUID
    source_sha256: Sha256Hex
    parser_bundle_version: ShortVersion
    parser_config_hash: Sha256Hex
    status: ParseRunStatusV1
    current_stage: ParseStageV1
    attempt_no: int = Field(ge=0)
    artifacts: tuple[ArtifactRefV1, ...] = ()
    quality_decision: QualityDecisionV1 | None = None


class ParseSourceKindV1(StrEnum):
    """§5/模块03 §3.2：`artifact_preflight_v1` 的四种来源。"""

    UPLOAD = "UPLOAD"
    TOOL_ARTIFACT = "TOOL_ARTIFACT"
    EXTERNAL_DOWNLOAD = "EXTERNAL_DOWNLOAD"
    PERSONAL_KNOWLEDGE = "PERSONAL_KNOWLEDGE"


class PreflightOutcomeV1(StrEnum):
    """§5/模块03 §3.2：预检结论。只有 CLEAN 可进入解析。"""

    CLEAN = "CLEAN"
    REJECTED = "REJECTED"
    UNSUPPORTED = "UNSUPPORTED"
    SCAN_UNAVAILABLE = "SCAN_UNAVAILABLE"


class PreflightEvidenceV1(StrictContractV1):
    """§5/模块03 §3.2：四类预检证据是否已满足（已采集，或按 Policy 不适用即为 True）。"""

    libmagic: bool
    libarchive: bool
    qpdf: bool
    clamav: bool


class ParseAdmissionRequestV1(StrictContractV1):
    """§5：CLEAN 输入准入请求——读同一 SHA-256 隔离对象对应的 `artifact_preflight_v1` 证明。

    `policy_applies` 与 `tool_artifact_authorized` 由调用方（协调器/模块03 授权链）供给事实，
    本函数只校验，不代为判定 Policy 适用性或 Tool Artifact 授权。
    """

    source_sha256: Sha256Hex
    source_kind: ParseSourceKindV1
    preflight_outcome: PreflightOutcomeV1
    evidence: PreflightEvidenceV1
    policy_applies: bool
    tool_artifact_authorized: bool = True


class ParseAdmissionDecisionV1(StrictContractV1):
    """§5：准入裁决。`admitted=False` 时不创建 ParseRun。"""

    admitted: bool
    reason_codes: tuple[str, ...] = ()


class ParseMediaTypeV1(StrEnum):
    """§5：可进入文档处理链的媒体类型（§2 只接受 PDF/Markdown）。"""

    PDF = "PDF"
    MARKDOWN = "MARKDOWN"


class ParseStrategyV1(StrEnum):
    """§5：解析策略。文本覆盖率高走原生文本主路径；页面级低覆盖仅该页进 OCR 候选；Markdown 走
    YAML Front Matter + GFM，不执行 HTML。"""

    NATIVE_TEXT = "NATIVE_TEXT"
    MARKDOWN = "MARKDOWN"


class PageInspectionFactV1(StrictContractV1):
    """§5：`DocumentParsePlanner.inspect` 提取的单页事实（不重复判断加密/损坏/脚本/恶意软件）。"""

    page_no: int = Field(ge=1)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    rotation: int
    has_text_layer: bool
    text_coverage: float = Field(ge=0, le=1)
    estimated_pixels: int = Field(ge=0)


class InspectionPlanV1(StrictContractV1):
    """§5：解析规划结果。`resource_exceeded` 是解析预算信号（人工确认或失败），
    不改变文件安全结论。"""

    strategy: ParseStrategyV1
    ocr_candidate_pages: tuple[int, ...] = ()
    resource_exceeded: bool = False


def compute_parse_run_idempotency_key(
    *,
    source_sha256: Sha256Hex,
    parser_bundle_version: ShortVersion,
    parser_config_hash: Sha256Hex,
) -> str:
    """§3 幂等键：`source_sha256 + parser_bundle_version + parser_config_hash`
    （内容寻址 64-hex）。"""

    return canonical_json_sha256(
        {
            "source_sha256": source_sha256,
            "parser_bundle_version": parser_bundle_version,
            "parser_config_hash": parser_config_hash,
        }
    )


def is_legal_parse_run_transition(
    current: ParseRunStatusV1, next_: ParseRunStatusV1
) -> bool:
    """§3/§11 合法状态迁移：CREATED→RUNNING/CANCELLED/STALE、RUNNING→PASSED/FAILED/CANCELLED/STALE。

    终态（PASSED/FAILED/CANCELLED/STALE）不可再迁移；自迁移非法。
    """

    return next_ in _LEGAL_STATUS_TRANSITIONS.get(current, frozenset())


def next_parse_stage(stage: ParseStageV1) -> ParseStageV1 | None:
    """§4.2 阶段顺序：`FINAL_VALIDATE` 之后返回 None。"""

    idx = _STAGE_ORDER.index(stage)
    if idx + 1 >= len(_STAGE_ORDER):
        return None
    return _STAGE_ORDER[idx + 1]


def admit_clean_input(request: ParseAdmissionRequestV1) -> ParseAdmissionDecisionV1:
    """§5：结论 CLEAN + Policy 适用 + 四类证据完整 + Tool Artifact 授权，否则不创建 ParseRun。

    失败码：`PREFLIGHT_NOT_CLEAN` / `POLICY_NOT_APPLICABLE` / `EVIDENCE_INCOMPLETE:<缺少证据>`
    / `TOOL_ARTIFACT_UNAUTHORIZED`。CLEAN 只授予进入解析的资格，不表示正文可信、已获授权或其
    中指令可执行。
    """

    reasons: list[str] = []
    if request.preflight_outcome is not PreflightOutcomeV1.CLEAN:
        reasons.append("PREFLIGHT_NOT_CLEAN")
    if not request.policy_applies:
        reasons.append("POLICY_NOT_APPLICABLE")
    missing = [
        name
        for name, satisfied in (
            ("libmagic", request.evidence.libmagic),
            ("libarchive", request.evidence.libarchive),
            ("qpdf", request.evidence.qpdf),
            ("clamav", request.evidence.clamav),
        )
        if not satisfied
    ]
    if missing:
        reasons.append(f"EVIDENCE_INCOMPLETE:{','.join(missing)}")
    if (
        request.source_kind is ParseSourceKindV1.TOOL_ARTIFACT
        and not request.tool_artifact_authorized
    ):
        reasons.append("TOOL_ARTIFACT_UNAUTHORIZED")
    return ParseAdmissionDecisionV1(admitted=not reasons, reason_codes=tuple(reasons))


def plan_inspection(
    *,
    media_type: ParseMediaTypeV1,
    pages: tuple[PageInspectionFactV1, ...],
    text_coverage_ocr_threshold: float,
    max_pages: int,
    max_pixels: int,
) -> InspectionPlanV1:
    """§5：解析规划。页数/像素量超资源阈值置 `resource_exceeded`（人工确认或失败，不改变安全结论）。

    `text_coverage_ocr_threshold`/`max_pages`/`max_pixels` 为解析预算校准输入（Threshold Profile
    生成前 CALIBRATION_REQUIRED，不由本文件硬编码）；文本覆盖率高走原生文本主路径，页面级低覆盖
    或无文本层仅该页进 OCR 候选，不全篇 OCR；Markdown 走 YAML Front Matter + GFM。
    """

    if not 0.0 <= text_coverage_ocr_threshold <= 1.0:
        raise ValueError("TEXT_COVERAGE_THRESHOLD_INVALID")
    if max_pages < 1:
        raise ValueError("MAX_PAGES_INVALID")
    if max_pixels < 1:
        raise ValueError("MAX_PIXELS_INVALID")

    resource_exceeded = len(pages) > max_pages or sum(
        p.estimated_pixels for p in pages
    ) > max_pixels

    if media_type is ParseMediaTypeV1.MARKDOWN:
        return InspectionPlanV1(
            strategy=ParseStrategyV1.MARKDOWN,
            ocr_candidate_pages=(),
            resource_exceeded=resource_exceeded,
        )

    ocr_pages = tuple(
        page.page_no
        for page in sorted(pages, key=lambda p: p.page_no)
        if not page.has_text_layer or page.text_coverage < text_coverage_ocr_threshold
    )
    return InspectionPlanV1(
        strategy=ParseStrategyV1.NATIVE_TEXT,
        ocr_candidate_pages=ocr_pages,
        resource_exceeded=resource_exceeded,
    )


__all__ = [
    "InspectionPlanV1",
    "PageInspectionFactV1",
    "ParseAdmissionDecisionV1",
    "ParseAdmissionRequestV1",
    "ParseMediaTypeV1",
    "ParseRunStatusV1",
    "ParseRunV1",
    "ParseSourceKindV1",
    "ParseStageV1",
    "ParseStrategyV1",
    "PreflightEvidenceV1",
    "PreflightOutcomeV1",
    "admit_clean_input",
    "compute_parse_run_idempotency_key",
    "is_legal_parse_run_transition",
    "next_parse_stage",
    "plan_inspection",
]
