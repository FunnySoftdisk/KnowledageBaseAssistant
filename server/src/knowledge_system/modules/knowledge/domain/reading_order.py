"""DEV-09：文档处理阅读顺序排序 + 局部 OCR 兜底区域计划（零 LLM/零 I/O）。

权威设计：模块05《文档处理模块详细设计》§6.3（阅读顺序）/§8.2（区域计划）。本文件只冻结：
- `compute_reading_order`：同栏内按 (y, x) 排序、跨栏标题先于正文、页眉页脚标记不进入正文顺序、
  顺序线交叉计数生成质量分（§6.3.1/§6.3.3/§6.3.5/§6.3.6）；
- `plan_region_fallback`：合并相邻问题区域并加 10%-15% 上下文边距，重叠区域按类型优先级
  公式/表格专用 -> OCR 通用（§8.2）。

列/栏区域识别、跨页页眉页脚文本重复检测、真实 OCR 调用与替换合并均属后续切片；本文件不访问
模型、网络、OCR、ORM 或 Temporal。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from knowledge_system.modules.tasking.domain.core_types import StrictContractV1

from .document import BlockTypeV1, BoundingBoxV1


class LayoutBlockV1(StrictContractV1):
    """§6.3 阅读顺序排序输入。列/跨栏/页眉页脚属性由布局分析供给，本模块只做确定性排序。"""

    block_id: str
    block_type: BlockTypeV1
    bbox: BoundingBoxV1
    text: str | None = None
    initial_order: int = Field(ge=0)
    column: int = Field(ge=0)
    spans_columns: bool = False
    is_header_footer: bool = False


class ReadingOrderResultV1(StrictContractV1):
    """§6.3 阅读顺序结果。`body` 为正文顺序块 ID，`decorations` 为页眉页脚块 ID（标记不参与正文）。

    `quality_score` 为顺序线交叉计数的确定性信号（§6.3.6），权重 CALIBRATION_REQUIRED，不冒充
    评测校准分。
    """

    body: tuple[str, ...]
    decorations: tuple[str, ...] = ()
    quality_score: float = Field(ge=0, le=1)
    quality_notes: tuple[str, ...] = ()


class RegionPriorityV1(StrEnum):
    """§8.2：重叠区域类型优先级，公式/表格专用高于 OCR 通用。"""

    FORMULA_TABLE = "FORMULA_TABLE"
    OCR_GENERIC = "OCR_GENERIC"


_PRIORITY_RANK: dict[RegionPriorityV1, int] = {
    RegionPriorityV1.FORMULA_TABLE: 1,
    RegionPriorityV1.OCR_GENERIC: 0,
}


class ProblemRegionV1(StrictContractV1):
    """§8.1/§8.2：触发兜底的问题区域。`reason` 为触发原因（乱码/表格空/阅读顺序异常等）。"""

    page_no: int = Field(ge=1)
    bbox: BoundingBoxV1
    priority: RegionPriorityV1
    reason: str


class RegionFallbackRequestV1(StrictContractV1):
    """§8.2：兜底区域计划输入。`margin_ratio` 为上下文边距比例（10%-15%，§8.2 固定范围）。"""

    page_width: float = Field(gt=0)
    page_height: float = Field(gt=0)
    margin_ratio: float = Field(ge=0.10, le=0.15)
    regions: tuple[ProblemRegionV1, ...] = ()


class RegionFallbackPlanV1(StrictContractV1):
    """§8.2：兜底区域计划——相邻问题区域合并为更少调用单元（已含上下文边距）。"""

    merged_regions: tuple[ProblemRegionV1, ...]


def _intersect(a: BoundingBoxV1, b: BoundingBoxV1) -> bool:
    return not (a.x1 <= b.x0 or b.x1 <= a.x0 or a.y1 <= b.y0 or b.y1 <= a.y0)


def _merge_reasons(a: str, b: str) -> str:
    return "; ".join(dict.fromkeys(part for part in (a, b) if part))


def _expand(
    region: ProblemRegionV1,
    *,
    page_width: float,
    page_height: float,
    margin_ratio: float,
) -> ProblemRegionV1:
    dx = (region.bbox.x1 - region.bbox.x0) * margin_ratio
    dy = (region.bbox.y1 - region.bbox.y0) * margin_ratio
    return ProblemRegionV1(
        page_no=region.page_no,
        bbox=BoundingBoxV1(
            x0=max(0.0, region.bbox.x0 - dx),
            y0=max(0.0, region.bbox.y0 - dy),
            x1=min(page_width, region.bbox.x1 + dx),
            y1=min(page_height, region.bbox.y1 + dy),
        ),
        priority=region.priority,
        reason=region.reason,
    )


def compute_reading_order(
    blocks: tuple[LayoutBlockV1, ...],
    *,
    column_order: tuple[int, ...] = (),
) -> ReadingOrderResultV1:
    """§6.3 确定性阅读顺序排序（列/跨栏/页眉页脚属性由布局分析供给，不在此重导）。

    - 页眉页脚（`is_header_footer`）标记但不进入正文顺序（§6.3.5），返回于 `decorations`；
    - 跨栏块（`spans_columns`）先于正文、按 (y0, x0, initial_order) 升序（§6.3.3「跨栏标题先于其
      覆盖栏」的确定性简化，精确按覆盖栏交错需列几何，留布局分析步骤）；
    - 同栏内按 (y0, x0, initial_order) 排序（§6.3.3，主解析器顺序作平局序）；
    - 表格/图/图注邻接组经同栏分组自然保持（§6.3.4），不拆入其他栏；
    - `quality_score` 按同栏相邻正文块的纵向区间重叠（并列歧义）计数（§6.3.6）。
    """

    decorations = sorted(
        (b for b in blocks if b.is_header_footer),
        key=lambda b: (b.initial_order, b.bbox.y0, b.bbox.x0),
    )
    body = [b for b in blocks if not b.is_header_footer]

    spanning = sorted(
        (b for b in body if b.spans_columns),
        key=lambda b: (b.bbox.y0, b.bbox.x0, b.initial_order),
    )
    columned = [b for b in body if not b.spans_columns]

    if column_order:
        rank = {c: i for i, c in enumerate(column_order)}
        column_indices = sorted(
            {b.column for b in columned},
            key=lambda c: (rank.get(c, len(rank)), c),
        )
    else:
        column_indices = sorted({b.column for b in columned})

    column_sorted: list[LayoutBlockV1] = []
    for column in column_indices:
        column_sorted.extend(
            sorted(
                (b for b in columned if b.column == column),
                key=lambda b: (b.bbox.y0, b.bbox.x0, b.initial_order),
            )
        )

    ordered = [*spanning, *column_sorted]

    crossings = 0
    for prev, cur in zip(ordered, ordered[1:], strict=False):
        if prev.spans_columns or cur.spans_columns:
            continue
        if (
            prev.column == cur.column
            and prev.bbox.y0 < cur.bbox.y0 < prev.bbox.y1
        ):
            crossings += 1

    notes: tuple[str, ...] = ()
    if crossings:
        notes = (f"ORDER_LINE_CROSSINGS={crossings}",)

    return ReadingOrderResultV1(
        body=tuple(b.block_id for b in ordered),
        decorations=tuple(b.block_id for b in decorations),
        quality_score=max(0.0, 1.0 - 0.1 * crossings),
        quality_notes=notes,
    )


def plan_region_fallback(
    request: RegionFallbackRequestV1,
) -> RegionFallbackPlanV1:
    """§8.2：合并相邻（重叠/相触）问题区域并加上下文边距，避免逐小块调用。

    同页重叠区域按类型优先级取高（公式/表格专用 -> OCR 通用）；合并后每区按 `margin_ratio`
    （区域自身宽高比例）向外扩展并裁剪到页面边界。
    """

    regions = sorted(
        request.regions, key=lambda r: (r.page_no, r.bbox.y0, r.bbox.x0)
    )
    merged: list[ProblemRegionV1] = []
    for region in regions:
        tail = merged[-1] if merged else None
        if (
            tail is None
            or tail.page_no != region.page_no
            or not _intersect(tail.bbox, region.bbox)
        ):
            merged.append(region)
            continue
        priority = (
            tail.priority
            if _PRIORITY_RANK[tail.priority] >= _PRIORITY_RANK[region.priority]
            else region.priority
        )
        merged[-1] = ProblemRegionV1(
            page_no=tail.page_no,
            bbox=BoundingBoxV1(
                x0=min(tail.bbox.x0, region.bbox.x0),
                y0=min(tail.bbox.y0, region.bbox.y0),
                x1=max(tail.bbox.x1, region.bbox.x1),
                y1=max(tail.bbox.y1, region.bbox.y1),
            ),
            priority=priority,
            reason=_merge_reasons(tail.reason, region.reason),
        )

    return RegionFallbackPlanV1(
        merged_regions=tuple(
            _expand(
                region,
                page_width=request.page_width,
                page_height=request.page_height,
                margin_ratio=request.margin_ratio,
            )
            for region in merged
        )
    )


__all__ = [
    "LayoutBlockV1",
    "ProblemRegionV1",
    "ReadingOrderResultV1",
    "RegionFallbackPlanV1",
    "RegionFallbackRequestV1",
    "RegionPriorityV1",
    "compute_reading_order",
    "plan_region_fallback",
]
