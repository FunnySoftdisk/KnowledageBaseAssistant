"""DEV-09 子批次 2：解析运行模型 + CLEAN 准入 + 解析规划 + 阅读顺序 + 兜底区域计划的离线盲测。"""

from __future__ import annotations

import unittest
from uuid import uuid4

from pydantic import ValidationError

from knowledge_system.modules.knowledge.public import (
    BlockTypeV1,
    BoundingBoxV1,
    LayoutBlockV1,
    PageInspectionFactV1,
    ParseAdmissionDecisionV1,
    ParseAdmissionRequestV1,
    ParseMediaTypeV1,
    ParseRunStatusV1,
    ParseRunV1,
    ParseSourceKindV1,
    ParseStageV1,
    ParseStrategyV1,
    PreflightEvidenceV1,
    PreflightOutcomeV1,
    ProblemRegionV1,
    QualityDecisionV1,
    ReadingOrderResultV1,
    RegionFallbackPlanV1,
    RegionFallbackRequestV1,
    RegionPriorityV1,
    admit_clean_input,
    compute_parse_run_idempotency_key,
    compute_reading_order,
    is_legal_parse_run_transition,
    next_parse_stage,
    plan_inspection,
    plan_region_fallback,
)
from knowledge_system.modules.tasking.domain.core_types import ArtifactRefV1

DIGEST = "a" * 64


def _bbox(
    x0: float = 1.0, y0: float = 2.0, x1: float = 100.0, y1: float = 200.0
) -> BoundingBoxV1:
    return BoundingBoxV1(x0=x0, y0=y0, x1=x1, y1=y1)


def _artifact() -> ArtifactRefV1:
    return ArtifactRefV1(
        artifact_id=uuid4(),
        artifact_type="canonical_document",
        sha256=DIGEST,
        schema_id="canonical_document",
        schema_version="1",
    )


def _run(**overrides: object) -> ParseRunV1:
    fields: dict[str, object] = {
        "id": uuid4(),
        "knowledge_version_id": uuid4(),
        "source_sha256": DIGEST,
        "parser_bundle_version": "1.0.0",
        "parser_config_hash": DIGEST,
        "status": ParseRunStatusV1.CREATED,
        "current_stage": ParseStageV1.ADMIT_CLEAN_INPUT,
        "attempt_no": 0,
    }
    fields.update(overrides)
    return ParseRunV1(**fields)


def _admission_request(**overrides: object) -> ParseAdmissionRequestV1:
    fields: dict[str, object] = {
        "source_sha256": DIGEST,
        "source_kind": ParseSourceKindV1.UPLOAD,
        "preflight_outcome": PreflightOutcomeV1.CLEAN,
        "evidence": PreflightEvidenceV1(
            libmagic=True, libarchive=True, qpdf=True, clamav=True
        ),
        "policy_applies": True,
    }
    fields.update(overrides)
    return ParseAdmissionRequestV1(**fields)


def _page(
    page_no: int,
    *,
    has_text_layer: bool = True,
    coverage: float = 0.9,
    pixels: int = 1_000,
) -> PageInspectionFactV1:
    return PageInspectionFactV1(
        page_no=page_no,
        width=612.0,
        height=792.0,
        rotation=0,
        has_text_layer=has_text_layer,
        text_coverage=coverage,
        estimated_pixels=pixels,
    )


def _layout(
    block_id: str,
    *,
    bbox: BoundingBoxV1,
    column: int = 0,
    spans: bool = False,
    header: bool = False,
    initial: int = 0,
    block_type: BlockTypeV1 = BlockTypeV1.PARAGRAPH,
) -> LayoutBlockV1:
    return LayoutBlockV1(
        block_id=block_id,
        block_type=block_type,
        bbox=bbox,
        initial_order=initial,
        column=column,
        spans_columns=spans,
        is_header_footer=header,
    )


class ParseStageV1Tests(unittest.TestCase):
    def test_nine_stages_in_order(self) -> None:
        self.assertEqual(
            [s.value for s in ParseStageV1],
            [
                "ADMIT_CLEAN_INPUT",
                "PARSE_PLANNING",
                "PRIMARY_PARSE",
                "ACADEMIC_ENRICH",
                "QUALITY_SCAN",
                "REGION_FALLBACK",
                "CANONICALIZE",
                "SERIALIZE",
                "FINAL_VALIDATE",
            ],
        )

    def test_next_parse_stage_chain(self) -> None:
        stages = list(ParseStageV1)
        for current, expected in zip(stages, stages[1:], strict=False):
            self.assertEqual(next_parse_stage(current), expected)
        self.assertIsNone(next_parse_stage(ParseStageV1.FINAL_VALIDATE))


class ParseRunStatusV1Tests(unittest.TestCase):
    def test_six_values(self) -> None:
        self.assertEqual(
            {s.value for s in ParseRunStatusV1},
            {"CREATED", "RUNNING", "PASSED", "FAILED", "CANCELLED", "STALE"},
        )

    def test_legal_transitions(self) -> None:
        self.assertTrue(
            is_legal_parse_run_transition(
                ParseRunStatusV1.CREATED, ParseRunStatusV1.RUNNING
            )
        )
        self.assertTrue(
            is_legal_parse_run_transition(
                ParseRunStatusV1.RUNNING, ParseRunStatusV1.PASSED
            )
        )
        self.assertTrue(
            is_legal_parse_run_transition(
                ParseRunStatusV1.RUNNING, ParseRunStatusV1.STALE
            )
        )

    def test_terminal_has_no_outgoing(self) -> None:
        for terminal in (
            ParseRunStatusV1.PASSED,
            ParseRunStatusV1.FAILED,
            ParseRunStatusV1.CANCELLED,
            ParseRunStatusV1.STALE,
        ):
            for other in ParseRunStatusV1:
                self.assertFalse(is_legal_parse_run_transition(terminal, other))

    def test_self_transition_illegal(self) -> None:
        self.assertFalse(
            is_legal_parse_run_transition(
                ParseRunStatusV1.CREATED, ParseRunStatusV1.CREATED
            )
        )


class ParseRunV1Tests(unittest.TestCase):
    def test_valid_run(self) -> None:
        run = _run(quality_decision=QualityDecisionV1.REVIEW_REQUIRED)
        self.assertEqual(run.status, ParseRunStatusV1.CREATED)
        self.assertEqual(run.quality_decision, QualityDecisionV1.REVIEW_REQUIRED)
        self.assertEqual(run.artifacts, ())

    def test_artifacts_typed(self) -> None:
        run = _run(artifacts=(_artifact(),))
        self.assertEqual(len(run.artifacts), 1)

    def test_extra_forbidden(self) -> None:
        with self.assertRaises(ValidationError):
            ParseRunV1(**_run().model_dump(), extra=1)  # type: ignore[call-arg]

    def test_attempt_no_negative(self) -> None:
        with self.assertRaises(ValidationError):
            _run(attempt_no=-1)


class ComputeParseRunIdempotencyKeyTests(unittest.TestCase):
    def test_deterministic(self) -> None:
        args = dict(
            source_sha256=DIGEST,
            parser_bundle_version="1.0.0",
            parser_config_hash=DIGEST,
        )
        self.assertEqual(
            compute_parse_run_idempotency_key(**args),
            compute_parse_run_idempotency_key(**args),
        )

    def test_64_hex(self) -> None:
        key = compute_parse_run_idempotency_key(
            source_sha256=DIGEST,
            parser_bundle_version="1.0.0",
            parser_config_hash=DIGEST,
        )
        self.assertRegex(key, r"^[0-9a-f]{64}$")

    def test_differs_on_each_component(self) -> None:
        base = dict(
            source_sha256=DIGEST,
            parser_bundle_version="1.0.0",
            parser_config_hash=DIGEST,
        )
        other_digest = "b" * 64
        self.assertNotEqual(
            compute_parse_run_idempotency_key(**base),
            compute_parse_run_idempotency_key(**{**base, "source_sha256": other_digest}),
        )
        self.assertNotEqual(
            compute_parse_run_idempotency_key(**base),
            compute_parse_run_idempotency_key(**{**base, "parser_bundle_version": "1.0.1"}),
        )
        self.assertNotEqual(
            compute_parse_run_idempotency_key(**base),
            compute_parse_run_idempotency_key(**{**base, "parser_config_hash": other_digest}),
        )


class AdmitCleanInputTests(unittest.TestCase):
    def test_clean_admits(self) -> None:
        decision = admit_clean_input(_admission_request())
        self.assertEqual(decision, ParseAdmissionDecisionV1(admitted=True))

    def test_not_clean_rejects(self) -> None:
        decision = admit_clean_input(
            _admission_request(preflight_outcome=PreflightOutcomeV1.REJECTED)
        )
        self.assertFalse(decision.admitted)
        self.assertIn("PREFLIGHT_NOT_CLEAN", decision.reason_codes)

    def test_policy_not_applicable_rejects(self) -> None:
        decision = admit_clean_input(_admission_request(policy_applies=False))
        self.assertFalse(decision.admitted)
        self.assertIn("POLICY_NOT_APPLICABLE", decision.reason_codes)

    def test_incomplete_evidence_names_missing(self) -> None:
        decision = admit_clean_input(
            _admission_request(
                evidence=PreflightEvidenceV1(
                    libmagic=True, libarchive=True, qpdf=True, clamav=False
                )
            )
        )
        self.assertFalse(decision.admitted)
        self.assertIn("EVIDENCE_INCOMPLETE:clamav", decision.reason_codes)

    def test_tool_artifact_unauthorized_rejects(self) -> None:
        decision = admit_clean_input(
            _admission_request(
                source_kind=ParseSourceKindV1.TOOL_ARTIFACT,
                tool_artifact_authorized=False,
            )
        )
        self.assertFalse(decision.admitted)
        self.assertIn("TOOL_ARTIFACT_UNAUTHORIZED", decision.reason_codes)

    def test_tool_artifact_authorized_admits(self) -> None:
        decision = admit_clean_input(
            _admission_request(
                source_kind=ParseSourceKindV1.TOOL_ARTIFACT,
                tool_artifact_authorized=True,
            )
        )
        self.assertTrue(decision.admitted)


class PlanInspectionTests(unittest.TestCase):
    def test_markdown_strategy_no_ocr(self) -> None:
        plan = plan_inspection(
            media_type=ParseMediaTypeV1.MARKDOWN,
            pages=(_page(1), _page(2)),
            text_coverage_ocr_threshold=0.5,
            max_pages=10,
            max_pixels=1_000_000,
        )
        self.assertEqual(plan.strategy, ParseStrategyV1.MARKDOWN)
        self.assertEqual(plan.ocr_candidate_pages, ())

    def test_native_text_ocr_candidates_by_page(self) -> None:
        plan = plan_inspection(
            media_type=ParseMediaTypeV1.PDF,
            pages=(
                _page(1, coverage=0.8),
                _page(2, coverage=0.3),
                _page(3, has_text_layer=False, coverage=0.0),
            ),
            text_coverage_ocr_threshold=0.5,
            max_pages=10,
            max_pixels=1_000_000,
        )
        self.assertEqual(plan.strategy, ParseStrategyV1.NATIVE_TEXT)
        self.assertEqual(plan.ocr_candidate_pages, (2, 3))

    def test_resource_exceeded_by_pages(self) -> None:
        plan = plan_inspection(
            media_type=ParseMediaTypeV1.PDF,
            pages=(_page(1), _page(2)),
            text_coverage_ocr_threshold=0.5,
            max_pages=1,
            max_pixels=1_000_000,
        )
        self.assertTrue(plan.resource_exceeded)

    def test_resource_exceeded_by_pixels(self) -> None:
        plan = plan_inspection(
            media_type=ParseMediaTypeV1.PDF,
            pages=(_page(1, pixels=500), _page(2, pixels=600)),
            text_coverage_ocr_threshold=0.5,
            max_pages=10,
            max_pixels=1_000,
        )
        self.assertTrue(plan.resource_exceeded)

    def test_invalid_threshold_raises(self) -> None:
        with self.assertRaises(ValueError):
            plan_inspection(
                media_type=ParseMediaTypeV1.PDF,
                pages=(),
                text_coverage_ocr_threshold=1.5,
                max_pages=10,
                max_pixels=1_000_000,
            )

    def test_empty_pages(self) -> None:
        plan = plan_inspection(
            media_type=ParseMediaTypeV1.PDF,
            pages=(),
            text_coverage_ocr_threshold=0.5,
            max_pages=10,
            max_pixels=1_000_000,
        )
        self.assertEqual(plan.ocr_candidate_pages, ())
        self.assertFalse(plan.resource_exceeded)


class ComputeReadingOrderTests(unittest.TestCase):
    def _blocks(self) -> tuple[LayoutBlockV1, ...]:
        title = _layout(
            "title",
            bbox=_bbox(0, 0, 500, 30),
            spans=True,
            block_type=BlockTypeV1.TITLE,
        )
        header = _layout(
            "header",
            bbox=_bbox(0, 0, 500, 10),
            header=True,
            block_type=BlockTypeV1.HEADER_FOOTER,
        )
        a = _layout("a", bbox=_bbox(0, 40, 240, 80), column=0)
        b = _layout("b", bbox=_bbox(0, 100, 240, 140), column=0)
        c = _layout("c", bbox=_bbox(260, 50, 500, 90), column=1)
        d = _layout("d", bbox=_bbox(260, 110, 500, 150), column=1)
        return (d, b, c, a, title, header)

    def test_spanning_first_then_columns_by_yx(self) -> None:
        result = compute_reading_order(self._blocks())
        self.assertEqual(result.body, ("title", "a", "b", "c", "d"))
        self.assertEqual(result.decorations, ("header",))

    def test_column_order_override(self) -> None:
        result = compute_reading_order(self._blocks(), column_order=(1, 0))
        self.assertEqual(result.body, ("title", "c", "d", "a", "b"))

    def test_within_column_sorted_by_y_then_x(self) -> None:
        blocks = (
            _layout("right", bbox=_bbox(150, 40, 300, 80), column=0),
            _layout("left", bbox=_bbox(0, 40, 140, 80), column=0),
        )
        result = compute_reading_order(blocks)
        self.assertEqual(result.body, ("left", "right"))

    def test_overlap_crossing_lowers_score(self) -> None:
        blocks = (
            _layout("one", bbox=_bbox(0, 10, 100, 40), column=0),
            _layout("two", bbox=_bbox(0, 30, 100, 60), column=0),
        )
        result = compute_reading_order(blocks)
        self.assertEqual(result.body, ("one", "two"))
        self.assertLess(result.quality_score, 1.0)
        self.assertIn("ORDER_LINE_CROSSINGS=1", result.quality_notes)

    def test_no_crossing_full_score(self) -> None:
        blocks = (
            _layout("one", bbox=_bbox(0, 10, 100, 30), column=0),
            _layout("two", bbox=_bbox(0, 40, 100, 60), column=0),
        )
        result = compute_reading_order(blocks)
        self.assertEqual(result.quality_score, 1.0)
        self.assertEqual(result.quality_notes, ())

    def test_empty_blocks(self) -> None:
        result = compute_reading_order(())
        self.assertEqual(result, ReadingOrderResultV1(body=(), quality_score=1.0))


class PlanRegionFallbackTests(unittest.TestCase):
    def _region(
        self,
        bbox: BoundingBoxV1,
        *,
        page_no: int = 1,
        priority: RegionPriorityV1 = RegionPriorityV1.OCR_GENERIC,
        reason: str = "garbled",
    ) -> ProblemRegionV1:
        return ProblemRegionV1(page_no=page_no, bbox=bbox, priority=priority, reason=reason)

    def _request(self, *regions: ProblemRegionV1) -> RegionFallbackRequestV1:
        return RegionFallbackRequestV1(
            page_width=612.0, page_height=792.0, margin_ratio=0.1, regions=regions
        )

    def test_non_adjacent_not_merged(self) -> None:
        plan = plan_region_fallback(
            self._request(
                self._region(_bbox(0, 0, 10, 10)),
                self._region(_bbox(100, 100, 110, 110)),
            )
        )
        self.assertEqual(len(plan.merged_regions), 2)

    def test_overlapping_merged(self) -> None:
        plan = plan_region_fallback(
            self._request(
                self._region(_bbox(0, 0, 20, 20)),
                self._region(_bbox(10, 10, 30, 30)),
            )
        )
        self.assertEqual(len(plan.merged_regions), 1)
        merged = plan.merged_regions[0]
        # 合并 (0,0,30,30) 后按 0.1 边距扩展 -> (0,0,33,33)
        self.assertEqual((merged.bbox.x0, merged.bbox.y0), (0.0, 0.0))
        self.assertAlmostEqual(merged.bbox.x1, 33.0)
        self.assertAlmostEqual(merged.bbox.y1, 33.0)

    def test_overlap_priority_formula_table_wins(self) -> None:
        plan = plan_region_fallback(
            self._request(
                self._region(_bbox(0, 0, 20, 20), priority=RegionPriorityV1.OCR_GENERIC),
                self._region(_bbox(10, 10, 30, 30), priority=RegionPriorityV1.FORMULA_TABLE),
            )
        )
        self.assertEqual(
            plan.merged_regions[0].priority, RegionPriorityV1.FORMULA_TABLE
        )

    def test_reasons_merged_deduped(self) -> None:
        plan = plan_region_fallback(
            self._request(
                self._region(_bbox(0, 0, 20, 20), reason="garbled"),
                self._region(_bbox(10, 10, 30, 30), reason="table_empty"),
            )
        )
        self.assertEqual(plan.merged_regions[0].reason, "garbled; table_empty")

    def test_margin_expansion_clipped(self) -> None:
        # bbox (0,0,10,10), margin 0.1 -> dx=dy=1, left/top clip to 0, right/bottom -> 11.
        plan = plan_region_fallback(
            self._request(self._region(_bbox(0, 0, 10, 10)))
        )
        expanded = plan.merged_regions[0].bbox
        self.assertEqual((expanded.x0, expanded.y0), (0.0, 0.0))
        self.assertAlmostEqual(expanded.x1, 11.0)
        self.assertAlmostEqual(expanded.y1, 11.0)

    def test_different_pages_not_merged(self) -> None:
        plan = plan_region_fallback(
            self._request(
                self._region(_bbox(0, 0, 10, 10), page_no=1),
                self._region(_bbox(0, 0, 10, 10), page_no=2),
            )
        )
        self.assertEqual(len(plan.merged_regions), 2)

    def test_empty_regions(self) -> None:
        plan = plan_region_fallback(self._request())
        self.assertEqual(plan, RegionFallbackPlanV1(merged_regions=()))

    def test_margin_ratio_out_of_range(self) -> None:
        with self.assertRaises(ValidationError):
            RegionFallbackRequestV1(
                page_width=612.0,
                page_height=792.0,
                margin_ratio=0.5,
                regions=(),
            )


if __name__ == "__main__":
    unittest.main()
