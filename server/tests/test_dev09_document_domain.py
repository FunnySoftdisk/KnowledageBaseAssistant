"""DEV-09 子批次 1：文档处理领域契约与确定性归一化/质量门禁的离线盲测（零 LLM/零 I/O）。"""

from __future__ import annotations

import unittest
from uuid import uuid4

from pydantic import ValidationError

from knowledge_system.modules.knowledge.public import (
    AcademicMetadataV1,
    BlockTypeV1,
    BoundingBoxV1,
    CanonicalBlockV1,
    CanonicalDocumentV1,
    CanonicalPageV1,
    DocumentQualityReportV1,
    ParserManifestV1,
    QualityDecisionV1,
    QualityPolicyV1,
    TaskArtifactCanonicalDocumentV1,
    compute_block_id,
    decide_quality,
    normalize_bbox,
)

DIGEST = "a" * 64


def _bbox(
    x0: float = 1.0, y0: float = 2.0, x1: float = 100.0, y1: float = 200.0
) -> BoundingBoxV1:
    return BoundingBoxV1(x0=x0, y0=y0, x1=x1, y1=y1)


def _block(**overrides: object) -> CanonicalBlockV1:
    fields: dict[str, object] = {
        "block_id": "b1",
        "block_type": BlockTypeV1.PARAGRAPH,
        "reading_order": 0,
        "bbox": _bbox(),
        "text": "hello",
        "confidence": 0.9,
        "source_parser": "docling",
    }
    fields.update(overrides)
    return CanonicalBlockV1(**fields)


def _page() -> CanonicalPageV1:
    return CanonicalPageV1(
        page_no=1, width=612.0, height=792.0, rotation=0, blocks=(_block(),)
    )


def _quality(**overrides: object) -> DocumentQualityReportV1:
    fields: dict[str, object] = {
        "text_coverage": 1.0,
        "garble_rate": 1.0,
        "reading_order": 1.0,
        "table_completeness": 1.0,
        "formula_completeness": 1.0,
        "caption_association": 1.0,
        "citation_structure": 1.0,
        "locator_validity": 1.0,
        "composite_score": 1.0,
    }
    fields.update(overrides)
    return DocumentQualityReportV1(**fields)


def _doc() -> CanonicalDocumentV1:
    return CanonicalDocumentV1(
        schema_version=1,
        document_id=uuid4(),
        file_version_id=uuid4(),
        source_sha256=DIGEST,
        parser_manifest=ParserManifestV1(name="docling", version="1.0", config_hash=DIGEST),
        metadata=AcademicMetadataV1(),
        pages=(_page(),),
        quality=_quality(),
    )


class BlockTypeV1Tests(unittest.TestCase):
    def test_all_twelve_values(self) -> None:
        self.assertEqual(
            {m.value for m in BlockTypeV1},
            {
                "TITLE",
                "HEADING",
                "PARAGRAPH",
                "LIST_ITEM",
                "TABLE",
                "FORMULA",
                "FIGURE",
                "CAPTION",
                "FOOTNOTE",
                "REFERENCE",
                "HEADER_FOOTER",
                "UNKNOWN",
            },
        )


class QualityDecisionV1Tests(unittest.TestCase):
    def test_three_values(self) -> None:
        self.assertEqual(
            {m.value for m in QualityDecisionV1},
            {"PASS", "REVIEW_REQUIRED", "FAIL"},
        )


class BoundingBoxV1Tests(unittest.TestCase):
    def test_valid(self) -> None:
        bbox = _bbox()
        self.assertEqual((bbox.x0, bbox.y0, bbox.x1, bbox.y1), (1.0, 2.0, 100.0, 200.0))

    def test_x_order_invalid(self) -> None:
        with self.assertRaises(ValidationError):
            BoundingBoxV1(x0=100.0, y0=2.0, x1=1.0, y1=200.0)

    def test_y_order_invalid(self) -> None:
        with self.assertRaises(ValidationError):
            BoundingBoxV1(x0=1.0, y0=200.0, x1=100.0, y1=2.0)

    def test_extra_forbidden(self) -> None:
        with self.assertRaises(ValidationError):
            BoundingBoxV1(x0=1.0, y0=2.0, x1=100.0, y1=200.0, extra=1)  # type: ignore[call-arg]


class CanonicalBlockV1Tests(unittest.TestCase):
    def test_valid_with_defaults(self) -> None:
        block = _block(text=None)
        self.assertIsNone(block.text)
        self.assertIsNone(block.html)
        self.assertIsNone(block.latex)
        self.assertIsNone(block.parent_block_id)
        self.assertEqual(block.attributes, {})

    def test_confidence_out_of_range(self) -> None:
        with self.assertRaises(ValidationError):
            _block(confidence=1.5)

    def test_reading_order_negative(self) -> None:
        with self.assertRaises(ValidationError):
            _block(reading_order=-1)


class CanonicalPageV1Tests(unittest.TestCase):
    def test_page_no_min_one(self) -> None:
        with self.assertRaises(ValidationError):
            CanonicalPageV1(page_no=0, width=612.0, height=792.0, rotation=0)

    def test_width_positive(self) -> None:
        with self.assertRaises(ValidationError):
            CanonicalPageV1(page_no=1, width=0.0, height=792.0, rotation=0)


class CanonicalDocumentV1Tests(unittest.TestCase):
    def test_valid_document(self) -> None:
        doc = _doc()
        self.assertEqual(doc.schema_version, 1)
        self.assertEqual(len(doc.pages), 1)

    def test_source_sha256_pattern(self) -> None:
        with self.assertRaises(ValidationError):
            CanonicalDocumentV1(
                schema_version=1,
                document_id=uuid4(),
                file_version_id=uuid4(),
                source_sha256="not-a-hex",
                parser_manifest=ParserManifestV1(
                    name="docling", version="1.0", config_hash=DIGEST
                ),
                metadata=AcademicMetadataV1(),
                pages=(_page(),),
                quality=_quality(),
            )


class TaskArtifactCanonicalDocumentV1Tests(unittest.TestCase):
    def test_schema_version_literal(self) -> None:
        doc = TaskArtifactCanonicalDocumentV1(
            schema_version="task_artifact_canonical_document_v1",
            corpus_id=uuid4(),
            task_id=uuid4(),
            action_attempt_id=uuid4(),
            artifact_id=uuid4(),
            source_sha256=DIGEST,
            parser_manifest=ParserManifestV1(name="docling", version="1.0", config_hash=DIGEST),
            metadata=AcademicMetadataV1(),
            pages=(_page(),),
            quality=_quality(),
        )
        self.assertEqual(doc.schema_version, "task_artifact_canonical_document_v1")

    def test_schema_version_rejects_other_literal(self) -> None:
        with self.assertRaises(ValidationError):
            TaskArtifactCanonicalDocumentV1(
                schema_version="other",  # type: ignore[arg-type]
                corpus_id=uuid4(),
                task_id=uuid4(),
                action_attempt_id=uuid4(),
                artifact_id=uuid4(),
                source_sha256=DIGEST,
                parser_manifest=ParserManifestV1(
                    name="docling", version="1.0", config_hash=DIGEST
                ),
                metadata=AcademicMetadataV1(),
                pages=(_page(),),
                quality=_quality(),
            )


class ComputeBlockIdTests(unittest.TestCase):
    def test_deterministic(self) -> None:
        first = compute_block_id(
            source_sha256=DIGEST,
            page_no=1,
            block_type=BlockTypeV1.PARAGRAPH,
            quantized_bbox=_bbox(),
            normalized_text_prefix="hello",
        )
        second = compute_block_id(
            source_sha256=DIGEST,
            page_no=1,
            block_type=BlockTypeV1.PARAGRAPH,
            quantized_bbox=_bbox(),
            normalized_text_prefix="hello",
        )
        self.assertEqual(first, second)

    def test_24_char_hex(self) -> None:
        block_id = compute_block_id(
            source_sha256=DIGEST,
            page_no=1,
            block_type=BlockTypeV1.PARAGRAPH,
            quantized_bbox=_bbox(),
            normalized_text_prefix="hello",
        )
        self.assertEqual(len(block_id), 24)
        self.assertRegex(block_id, r"^[0-9a-f]{24}$")

    def test_differs_on_text_prefix(self) -> None:
        a = compute_block_id(
            source_sha256=DIGEST,
            page_no=1,
            block_type=BlockTypeV1.PARAGRAPH,
            quantized_bbox=_bbox(),
            normalized_text_prefix="hello",
        )
        b = compute_block_id(
            source_sha256=DIGEST,
            page_no=1,
            block_type=BlockTypeV1.PARAGRAPH,
            quantized_bbox=_bbox(),
            normalized_text_prefix="world",
        )
        self.assertNotEqual(a, b)

    def test_differs_on_page(self) -> None:
        a = compute_block_id(
            source_sha256=DIGEST,
            page_no=1,
            block_type=BlockTypeV1.PARAGRAPH,
            quantized_bbox=_bbox(),
            normalized_text_prefix="hello",
        )
        b = compute_block_id(
            source_sha256=DIGEST,
            page_no=2,
            block_type=BlockTypeV1.PARAGRAPH,
            quantized_bbox=_bbox(),
            normalized_text_prefix="hello",
        )
        self.assertNotEqual(a, b)

    def test_page_no_invalid(self) -> None:
        with self.assertRaises(ValueError):
            compute_block_id(
                source_sha256=DIGEST,
                page_no=0,
                block_type=BlockTypeV1.PARAGRAPH,
                quantized_bbox=_bbox(),
                normalized_text_prefix="hello",
            )


class NormalizeBboxTests(unittest.TestCase):
    def test_within_bounds_no_warning(self) -> None:
        result, warnings = normalize_bbox(
            _bbox(10.0, 20.0, 100.0, 200.0),
            page_width=612.0,
            page_height=792.0,
            tolerance=1.0,
        )
        self.assertEqual(result, _bbox(10.0, 20.0, 100.0, 200.0))
        self.assertEqual(warnings, ())

    def test_clip_within_tolerance(self) -> None:
        result, warnings = normalize_bbox(
            _bbox(-0.5, 20.0, 100.0, 200.0),
            page_width=612.0,
            page_height=792.0,
            tolerance=1.0,
        )
        self.assertEqual(result.x0, 0.0)
        self.assertIn("BBOX_X0_CLIPPED", warnings)

    def test_severe_out_of_bounds(self) -> None:
        result, warnings = normalize_bbox(
            _bbox(-50.0, 20.0, 100.0, 200.0),
            page_width=612.0,
            page_height=792.0,
            tolerance=1.0,
        )
        self.assertEqual(result.x0, 0.0)
        self.assertIn("BBOX_X0_SEVERE_OUT_OF_BOUNDS", warnings)

    def test_quantize_three_decimals(self) -> None:
        result, _ = normalize_bbox(
            _bbox(1.0, 2.0, 100.1234, 200.0),
            page_width=612.0,
            page_height=792.0,
            tolerance=1.0,
        )
        self.assertEqual(result.x1, 100.123)

    def test_fully_off_page_raises(self) -> None:
        with self.assertRaises(ValueError):
            normalize_bbox(
                _bbox(700.0, 20.0, 800.0, 200.0),
                page_width=612.0,
                page_height=792.0,
                tolerance=1.0,
            )

    def test_invalid_page_size(self) -> None:
        with self.assertRaises(ValueError):
            normalize_bbox(_bbox(), page_width=0.0, page_height=792.0, tolerance=1.0)


class DecideQualityTests(unittest.TestCase):
    def test_hard_failure_wins_over_composite(self) -> None:
        report = _quality(composite_score=1.0, hard_failures=("ZERO_VALID_BODY",))
        self.assertEqual(
            decide_quality(report, QualityPolicyV1(composite_pass_threshold=0.8)),
            QualityDecisionV1.FAIL,
        )

    def test_review_flag_wins_over_composite(self) -> None:
        report = _quality(composite_score=1.0, review_flags=("KEY_PAGE_BLANK",))
        self.assertEqual(
            decide_quality(report, QualityPolicyV1(composite_pass_threshold=0.8)),
            QualityDecisionV1.REVIEW_REQUIRED,
        )

    def test_key_page_blank_high_average_review(self) -> None:
        report = _quality(composite_score=0.95, review_flags=("KEY_PAGE_BLANK",))
        self.assertEqual(
            decide_quality(report, QualityPolicyV1(composite_pass_threshold=0.8)),
            QualityDecisionV1.REVIEW_REQUIRED,
        )

    def test_pass_above_threshold(self) -> None:
        report = _quality(composite_score=0.9)
        self.assertEqual(
            decide_quality(report, QualityPolicyV1(composite_pass_threshold=0.8)),
            QualityDecisionV1.PASS,
        )

    def test_below_threshold_review(self) -> None:
        report = _quality(composite_score=0.7)
        self.assertEqual(
            decide_quality(report, QualityPolicyV1(composite_pass_threshold=0.8)),
            QualityDecisionV1.REVIEW_REQUIRED,
        )


if __name__ == "__main__":
    unittest.main()
