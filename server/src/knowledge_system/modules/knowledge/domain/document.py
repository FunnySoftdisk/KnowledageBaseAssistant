"""DEV-09：文档处理领域契约与确定性归一化/质量门禁规则（零 LLM/零 embedding/零 I/O）。

权威设计：模块05《文档处理模块详细设计》§2（规范文档模型）/§6（块归一化）/§9（质量评估）。
本文件只冻结规范文档模型、块归一化与质量门禁的领域类型与确定性纯函数；不访问模型、网络、
OCR、ORM 或 Temporal。解析器适配（Docling/GROBID/PaddleOCR）、Markdown 解析与序列化 I/O、
ParseRun 运行模型与输入准入均属后续切片。
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from knowledge_system.modules.tasking.domain.core_types import Sha256Hex, StrictContractV1

_QUANTIZE_DECIMALS = 3
_NORMALIZED_TEXT_PREFIX_LIMIT = 64


class BlockTypeV1(StrEnum):
    """§2：规范块类型。"""

    TITLE = "TITLE"
    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    LIST_ITEM = "LIST_ITEM"
    TABLE = "TABLE"
    FORMULA = "FORMULA"
    FIGURE = "FIGURE"
    CAPTION = "CAPTION"
    FOOTNOTE = "FOOTNOTE"
    REFERENCE = "REFERENCE"
    HEADER_FOOTER = "HEADER_FOOTER"
    UNKNOWN = "UNKNOWN"


class QualityDecisionV1(StrEnum):
    """§9.2：质量门禁结论。"""

    PASS = "PASS"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAIL = "FAIL"


class BoundingBoxV1(StrictContractV1):
    """绝对 PDF 坐标的包围盒（原始页面左上角原点，§2「坐标统一为原始页面左上角原点」）。"""

    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def _validate_order(self) -> BoundingBoxV1:
        if self.x0 >= self.x1:
            raise ValueError("BOUNDING_BOX_X_ORDER_INVALID")
        if self.y0 >= self.y1:
            raise ValueError("BOUNDING_BOX_Y_ORDER_INVALID")
        return self


class CanonicalBlockV1(StrictContractV1):
    """§2：规范块。`text`/`html`/`latex` 是否至少其一非空由质量门禁裁决，不做 schema 强制。"""

    block_id: str
    block_type: BlockTypeV1
    reading_order: int = Field(ge=0)
    bbox: BoundingBoxV1
    text: str | None = None
    html: str | None = None
    latex: str | None = None
    parent_block_id: str | None = None
    confidence: float = Field(ge=0, le=1)
    source_parser: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class CanonicalPageV1(StrictContractV1):
    """§2：规范页。"""

    page_no: int = Field(ge=1)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    rotation: int
    blocks: tuple[CanonicalBlockV1, ...] = ()


class ParserManifestV1(StrictContractV1):
    """§2：解析器清单的最小冻结子集（设计未展开字段，实现时按需对齐模块05/模块00 后补）。"""

    name: str
    version: str
    config_hash: Sha256Hex


class AcademicMetadataV1(StrictContractV1):
    """§2/§7：学术元数据最小冻结子集。字段均可空，人工元数据最终由 Knowledge 模块合并。"""

    title: str | None = None
    authors: tuple[str, ...] = ()
    abstract: str | None = None
    doi: str | None = None


class DocumentQualityReportV1(StrictContractV1):
    """§9.1/§9.2：质量报告。八项指标各为 0..1（越高越好，`garble_rate` 已反转）。

    `hard_failures` 触发 FAIL（不可读/零正文/严重损坏/Artifact 校验失败）；
    `review_flags` 触发 REVIEW_REQUIRED（关键页/公式/表格低置信，正文可保留）；
    `warnings` 仅记录。综合分不能覆盖硬失败/硬复核条件（§9.2）。
    """

    text_coverage: float = Field(ge=0, le=1)
    garble_rate: float = Field(ge=0, le=1)
    reading_order: float = Field(ge=0, le=1)
    table_completeness: float = Field(ge=0, le=1)
    formula_completeness: float = Field(ge=0, le=1)
    caption_association: float = Field(ge=0, le=1)
    citation_structure: float = Field(ge=0, le=1)
    locator_validity: float = Field(ge=0, le=1)
    composite_score: float = Field(ge=0, le=1)
    hard_failures: tuple[str, ...] = ()
    review_flags: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class CanonicalDocumentV1(StrictContractV1):
    """§2：解析器无关的规范文档 JSON。"""

    schema_version: int = Field(ge=1)
    document_id: UUID
    file_version_id: UUID
    source_sha256: Sha256Hex
    parser_manifest: ParserManifestV1
    metadata: AcademicMetadataV1
    pages: tuple[CanonicalPageV1, ...]
    quality: DocumentQualityReportV1


class TaskArtifactCanonicalDocumentV1(StrictContractV1):
    """§2：`tool_artifact_evidence_v1` 专用，复用 Page/Block/质量模型，不含 KnowledgeVersionId。"""

    schema_version: Literal["task_artifact_canonical_document_v1"]
    corpus_id: UUID
    task_id: UUID
    action_attempt_id: UUID
    artifact_id: UUID
    source_sha256: Sha256Hex
    parser_manifest: ParserManifestV1
    metadata: AcademicMetadataV1
    pages: tuple[CanonicalPageV1, ...]
    quality: DocumentQualityReportV1


class QualityPolicyV1(StrictContractV1):
    """§9.2：质量门禁阈值。综合分阈值由 60 份校准集生成签名 Threshold Profile，
    生成前保持 CALIBRATION_REQUIRED；本契约只冻结决策函数输入形状。"""

    composite_pass_threshold: float = Field(ge=0, le=1)


def compute_block_id(
    *,
    source_sha256: Sha256Hex,
    page_no: int,
    block_type: BlockTypeV1,
    quantized_bbox: BoundingBoxV1,
    normalized_text_prefix: str,
) -> str:
    """§6.2 稳定 Block ID：`SHA256(规范化拼接)[:24]`，同一原文件+解析配置下稳定。"""

    if page_no < 1:
        raise ValueError("PAGE_NO_INVALID")
    prefix = normalized_text_prefix[:_NORMALIZED_TEXT_PREFIX_LIMIT]
    bbox = (
        f"{quantized_bbox.x0:.{_QUANTIZE_DECIMALS}f},"
        f"{quantized_bbox.y0:.{_QUANTIZE_DECIMALS}f},"
        f"{quantized_bbox.x1:.{_QUANTIZE_DECIMALS}f},"
        f"{quantized_bbox.y1:.{_QUANTIZE_DECIMALS}f}"
    )
    material = f"{source_sha256}|{page_no}|{block_type.value}|{bbox}|{prefix}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def normalize_bbox(
    bbox: BoundingBoxV1,
    *,
    page_width: float,
    page_height: float,
    tolerance: float,
) -> tuple[BoundingBoxV1, tuple[str, ...]]:
    """§6.1 确定性尾段：裁剪到页面边界 + 浮点量化（旋转/单位转换属适配器职责，§2）。

    越界坐标一律裁剪到 `[0, page_width]×[0, page_height]`；越界幅度超过 `tolerance` 记严重
    质量问题（`*_SEVERE_OUT_OF_BOUNDS`），否则记轻度裁剪告警（`*_CLIPPED`）。裁剪后仍退化
    （整块完全落在页面外）直接抛 `ValueError`，不静默构造非法 BBox。量化固定 3 位小数。
    """

    if page_width <= 0 or page_height <= 0:
        raise ValueError("PAGE_SIZE_INVALID")
    if tolerance < 0:
        raise ValueError("TOLERANCE_INVALID")

    warnings: list[str] = []

    def _clip(value: float, low: float, high: float, axis: str, side: str) -> float:
        if value < low:
            if value < low - tolerance:
                warnings.append(f"BBOX_{axis}{side}_SEVERE_OUT_OF_BOUNDS")
            else:
                warnings.append(f"BBOX_{axis}{side}_CLIPPED")
            return low
        if value > high:
            if value > high + tolerance:
                warnings.append(f"BBOX_{axis}{side}_SEVERE_OUT_OF_BOUNDS")
            else:
                warnings.append(f"BBOX_{axis}{side}_CLIPPED")
            return high
        return value

    x0 = _clip(bbox.x0, 0.0, page_width, "X", "0")
    y0 = _clip(bbox.y0, 0.0, page_height, "Y", "0")
    x1 = _clip(bbox.x1, 0.0, page_width, "X", "1")
    y1 = _clip(bbox.y1, 0.0, page_height, "Y", "1")

    quantized = BoundingBoxV1(
        x0=round(x0, _QUANTIZE_DECIMALS),
        y0=round(y0, _QUANTIZE_DECIMALS),
        x1=round(x1, _QUANTIZE_DECIMALS),
        y1=round(y1, _QUANTIZE_DECIMALS),
    )
    if quantized.x0 >= quantized.x1 or quantized.y0 >= quantized.y1:
        raise ValueError("BBOX_DEGENERATE_AFTER_CLIP")
    return quantized, tuple(warnings)


def decide_quality(
    report: DocumentQualityReportV1,
    policy: QualityPolicyV1,
) -> QualityDecisionV1:
    """§9.2：硬失败 > 硬复核 > 综合分阈值。综合分不能覆盖硬失败/硬复核条件。"""

    if report.hard_failures:
        return QualityDecisionV1.FAIL
    if report.review_flags:
        return QualityDecisionV1.REVIEW_REQUIRED
    if report.composite_score >= policy.composite_pass_threshold:
        return QualityDecisionV1.PASS
    return QualityDecisionV1.REVIEW_REQUIRED


__all__ = [
    "AcademicMetadataV1",
    "BlockTypeV1",
    "BoundingBoxV1",
    "CanonicalBlockV1",
    "CanonicalDocumentV1",
    "CanonicalPageV1",
    "DocumentQualityReportV1",
    "ParserManifestV1",
    "QualityDecisionV1",
    "QualityPolicyV1",
    "TaskArtifactCanonicalDocumentV1",
    "compute_block_id",
    "decide_quality",
    "normalize_bbox",
]
