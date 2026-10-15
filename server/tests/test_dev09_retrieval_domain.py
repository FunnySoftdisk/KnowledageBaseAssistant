"""DEV-09 子批次 4：检索服务查询与证据契约的离线盲测（零 LLM/零 embedding/零 I/O）。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError

from knowledge_system.modules.knowledge.public import (
    BlockCitationLocatorV1,
    CitationLocatorV1,
    CitationRefV1,
    EvidenceRepresentationV1,
    EvidenceSetV1,
    EvidenceSourceV1,
    EvidenceSufficiencyV1,
    EvidenceV1,
    ExternalEvidenceSourceV1,
    KnowledgeEvidenceSourceV1,
    PageCitationLocatorV1,
    QueryLanguageV1,
    RegionCitationLocatorV1,
    RowRangeCitationLocatorV1,
    RunArtifactCitationLocatorV1,
    ScoreBreakdownV1,
    SectionCitationLocatorV1,
    TaskAttachmentImageEvidenceSourceV1,
    TaskAttachmentStructuredEvidenceSourceV1,
    TaskAttachmentTextEvidenceSourceV1,
    TaskImageArtifactEvidenceSourceV1,
    TaskStructuredArtifactEvidenceSourceV1,
    TaskToolArtifactEvidenceSourceV1,
    UrlCitationLocatorV1,
    classify_query,
    lexical_terms,
    normalize_query,
)

DIGEST = "a" * 64
_EVIDENCE_SOURCE_ADAPTER = TypeAdapter(EvidenceSourceV1)
_LOCATOR_ADAPTER = TypeAdapter(CitationLocatorV1)


def _uids(n: int) -> tuple[UUID, ...]:
    return tuple(uuid4() for _ in range(n))


class EvidenceSourceV1Tests(unittest.TestCase):
    def test_ten_source_kinds_discriminate(self) -> None:
        c, t, a, g, art = _uids(5)
        cases = [
            (
                "KNOWLEDGE_VERSION",
                {"knowledge_version_id": c},
                KnowledgeEvidenceSourceV1,
            ),
            (
                "TASK_TOOL_ARTIFACT",
                {
                    "corpus_id": c, "task_id": t, "action_attempt_id": a,
                    "grant_id": g, "artifact_id": art, "source_sha256": DIGEST,
                },
                TaskToolArtifactEvidenceSourceV1,
            ),
            (
                "TASK_ATTACHMENT_TEXT",
                {
                    "corpus_id": c, "task_id": t, "attachment_binding_id": a,
                    "artifact_id": art, "source_sha256": DIGEST,
                },
                TaskAttachmentTextEvidenceSourceV1,
            ),
            (
                "TASK_STRUCTURED_ARTIFACT",
                {
                    "structured_query_id": c, "task_id": t, "action_attempt_id": a,
                    "grant_id": g, "artifact_id": art, "source_sha256": DIGEST,
                },
                TaskStructuredArtifactEvidenceSourceV1,
            ),
            (
                "TASK_ATTACHMENT_STRUCTURED",
                {
                    "structured_query_id": c, "task_id": t, "attachment_binding_id": a,
                    "artifact_id": art, "source_sha256": DIGEST,
                },
                TaskAttachmentStructuredEvidenceSourceV1,
            ),
            (
                "TASK_IMAGE_ARTIFACT",
                {
                    "image_evidence_run_id": c, "task_id": t, "action_attempt_id": a,
                    "grant_id": g, "artifact_id": art, "source_sha256": DIGEST,
                    "normalized_image_sha256": DIGEST,
                },
                TaskImageArtifactEvidenceSourceV1,
            ),
            (
                "TASK_ATTACHMENT_IMAGE",
                {
                    "image_evidence_run_id": c, "task_id": t, "attachment_binding_id": a,
                    "artifact_id": art, "source_sha256": DIGEST,
                    "normalized_image_sha256": DIGEST,
                },
                TaskAttachmentImageEvidenceSourceV1,
            ),
            (
                "EXTERNAL_WEB",
                {"external_capture_id": c, "task_id": t},
                ExternalEvidenceSourceV1,
            ),
            (
                "EXTERNAL_SCHOLARLY_RECORD",
                {"external_capture_id": c, "task_id": t},
                ExternalEvidenceSourceV1,
            ),
            (
                "EXTERNAL_CODE",
                {"external_capture_id": c, "task_id": t},
                ExternalEvidenceSourceV1,
            ),
        ]
        for kind, extra, expected in cases:
            with self.subTest(kind=kind):
                obj = _EVIDENCE_SOURCE_ADAPTER.validate_python(
                    {"source_kind": kind, **extra}
                )
                self.assertIsInstance(obj, expected)

    def test_unknown_source_kind_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _EVIDENCE_SOURCE_ADAPTER.validate_python(
                {"source_kind": "MADE_UP", "task_id": uuid4()}
            )

    def test_missing_field_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _EVIDENCE_SOURCE_ADAPTER.validate_python(
                {"source_kind": "KNOWLEDGE_VERSION"}
            )

    def test_extra_field_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _EVIDENCE_SOURCE_ADAPTER.validate_python(
                {
                    "source_kind": "KNOWLEDGE_VERSION",
                    "knowledge_version_id": uuid4(),
                    "extra": 1,
                }
            )


class CitationLocatorV1Tests(unittest.TestCase):
    def test_seven_locators_discriminate(self) -> None:
        u, v = _uids(2)
        cases = [
            ({"kind": "PAGE", "page_no": 3}, PageCitationLocatorV1),
            ({"kind": "SECTION", "section_path": ("1", "2")}, SectionCitationLocatorV1),
            ({"kind": "BLOCK", "block_id": "b1"}, BlockCitationLocatorV1),
            (
                {"kind": "ROW_RANGE", "row_from": 1, "row_to": 5, "parquet_row_groups": (0, 1)},
                RowRangeCitationLocatorV1,
            ),
            (
                {"kind": "REGION", "region_id": u, "bbox": (0.0, 0.0, 0.5, 0.5)},
                RegionCitationLocatorV1,
            ),
            (
                {
                    "kind": "URL",
                    "canonical_url": "https://example.com/a",
                    "accessed_at": datetime(2026, 1, 1, tzinfo=UTC),
                },
                UrlCitationLocatorV1,
            ),
            ({"kind": "RUN_ARTIFACT", "run_id": u, "artifact_id": v}, RunArtifactCitationLocatorV1),
        ]
        for payload, expected in cases:
            with self.subTest(kind=payload["kind"]):
                self.assertIsInstance(_LOCATOR_ADAPTER.validate_python(payload), expected)

    def test_section_path_cannot_be_empty(self) -> None:
        with self.assertRaises(ValidationError):
            _LOCATOR_ADAPTER.validate_python({"kind": "SECTION", "section_path": ()})

    def test_section_segment_too_long(self) -> None:
        with self.assertRaises(ValidationError):
            _LOCATOR_ADAPTER.validate_python(
                {"kind": "SECTION", "section_path": ("x" * 257,)}
            )

    def test_row_range_to_below_from(self) -> None:
        with self.assertRaises(ValidationError):
            _LOCATOR_ADAPTER.validate_python(
                {"kind": "ROW_RANGE", "row_from": 5, "row_to": 3}
            )

    def test_row_group_negative(self) -> None:
        with self.assertRaises(ValidationError):
            _LOCATOR_ADAPTER.validate_python(
                {"kind": "ROW_RANGE", "row_from": 1, "row_to": 3, "parquet_row_groups": (-1,)}
            )

    def test_region_bbox_out_of_range(self) -> None:
        with self.assertRaises(ValidationError):
            _LOCATOR_ADAPTER.validate_python(
                {"kind": "REGION", "region_id": uuid4(), "bbox": (0.0, 0.0, 1.5, 0.5)}
            )

    def test_region_bbox_x_not_increasing(self) -> None:
        with self.assertRaises(ValidationError):
            _LOCATOR_ADAPTER.validate_python(
                {"kind": "REGION", "region_id": uuid4(), "bbox": (0.5, 0.0, 0.5, 0.5)}
            )

    def test_url_must_be_valid(self) -> None:
        with self.assertRaises(ValidationError):
            _LOCATOR_ADAPTER.validate_python(
                {
                    "kind": "URL",
                    "canonical_url": "not-a-url",
                    "accessed_at": datetime.now(UTC),
                }
            )

    def test_unknown_locator_kind_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _LOCATOR_ADAPTER.validate_python({"kind": "FOO", "x": 1})


class NormalizeQueryTests(unittest.TestCase):
    def test_nfkc(self) -> None:
        self.assertEqual(normalize_query("ＡＢＣ").normalized_text, "ABC")

    def test_whitespace_compression(self) -> None:
        self.assertEqual(normalize_query("  a   b\n c ").normalized_text, "a b c")

    def test_case_preserved(self) -> None:
        self.assertEqual(normalize_query("Hello World").normalized_text, "Hello World")

    def test_original_preserved(self) -> None:
        nq = normalize_query("  raw   text  ")
        self.assertEqual(nq.original, "  raw   text  ")


class ClassifyQueryTests(unittest.TestCase):
    def _classify(self, text: str):
        return classify_query(normalize_query(text))

    def test_language_zh(self) -> None:
        self.assertEqual(self._classify("知识解析").language, QueryLanguageV1.ZH)

    def test_language_en(self) -> None:
        self.assertEqual(self._classify("RAG retrieval").language, QueryLanguageV1.EN)

    def test_language_mixed(self) -> None:
        self.assertEqual(self._classify("知识解析与RAG").language, QueryLanguageV1.MIXED)

    def test_exact_phrases(self) -> None:
        f = self._classify('use "exact phrase" here')
        self.assertEqual(f.exact_phrases, ("exact phrase",))

    def test_exact_phrases_cjk_brackets(self) -> None:
        f = self._classify("检索「知识解析」方法")
        self.assertEqual(f.exact_phrases, ("知识解析",))

    def test_doi(self) -> None:
        f = self._classify("paper 10.1145/1234567 cited")
        self.assertEqual(f.dois, ("10.1145/1234567",))

    def test_arxiv_id(self) -> None:
        f = self._classify("arXiv:2401.00001")
        self.assertEqual(f.arxiv_ids, ("2401.00001",))

    def test_arxiv_year_not_duplicated_as_year(self) -> None:
        f = self._classify("arXiv:2024.12345")
        self.assertEqual(f.arxiv_ids, ("2024.12345",))
        self.assertEqual(f.years, ())

    def test_years_sorted_deduped(self) -> None:
        f = self._classify("2024 2020 2024")
        self.assertEqual(f.years, (2020, 2024))

    def test_formula_numbers(self) -> None:
        f = self._classify("求解式(3) 与公式(2.1)")
        self.assertEqual(f.formula_numbers, ("3", "2.1"))

    def test_abbreviations(self) -> None:
        f = self._classify("RAG with FTS and BGE-M3")
        self.assertEqual(f.abbreviations, ("RAG", "FTS", "BGE"))

    def test_negated_true(self) -> None:
        self.assertTrue(self._classify("不含 2020 年的结果").negated)

    def test_negated_false(self) -> None:
        self.assertFalse(self._classify("RAG paper 2020").negated)

    def test_time_qualified_true(self) -> None:
        self.assertTrue(self._classify("近一年 RAG 进展").time_qualified)

    def test_time_qualified_false(self) -> None:
        self.assertFalse(self._classify("RAG paper").time_qualified)


class LexicalTermsTests(unittest.TestCase):
    def _terms(self, text: str, **kwargs: object):
        return lexical_terms(normalize_query(text), **kwargs)

    def test_simple_terms_lowercased(self) -> None:
        self.assertEqual(self._terms("BERT model 2023").simple_terms, ("bert", "model", "2023"))

    def test_simple_terms_deduped_ordered(self) -> None:
        self.assertEqual(self._terms("model model BERT").simple_terms, ("model", "bert"))

    def test_phrase_conditions(self) -> None:
        self.assertEqual(self._terms('a "hello world" b').phrase_conditions, ("hello world",))

    def test_chinese_english_passthrough(self) -> None:
        result = self._terms(
            "RAG 知识", chinese_terms=("知识",), english_stems=("retriev",)
        )
        self.assertEqual(result.chinese_terms, ("知识",))
        self.assertEqual(result.english_stems, ("retriev",))

    def test_defaults_empty(self) -> None:
        result = lexical_terms(normalize_query("hello"))
        self.assertEqual(result.chinese_terms, ())
        self.assertEqual(result.english_stems, ())


class EvidenceV1Tests(unittest.TestCase):
    def _evidence(self, **overrides: object) -> EvidenceV1:
        fields: dict[str, object] = {
            "evidence_kind": "TEXT",
            "evidence_id": "ev-1",
            "source": {
                "source_kind": "KNOWLEDGE_VERSION",
                "knowledge_version_id": uuid4(),
            },
            "parent_chunk_id": "parent-id",
            "child_chunk_ids": ("child-id",),
            "text": "body text",
            "content_hash": DIGEST,
            "token_count": 10,
            "representation": EvidenceRepresentationV1.FULL,
            "citations": (
                CitationRefV1(
                    locator=PageCitationLocatorV1(kind="PAGE", page_no=2)
                ),
            ),
            "scores": ScoreBreakdownV1(fusion_score=0.9, rerank_score=0.8),
        }
        fields.update(overrides)
        return EvidenceV1(**fields)

    def test_valid_text_evidence(self) -> None:
        ev = self._evidence()
        self.assertEqual(ev.evidence_kind, "TEXT")
        self.assertIsInstance(ev.source, KnowledgeEvidenceSourceV1)

    def test_content_hash_must_be_64_hex(self) -> None:
        with self.assertRaises(ValidationError):
            self._evidence(content_hash="not-a-hash")

    def test_negative_token_count(self) -> None:
        with self.assertRaises(ValidationError):
            self._evidence(token_count=-1)

    def test_non_text_kind_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self._evidence(evidence_kind="STRUCTURED")

    def test_extra_forbidden(self) -> None:
        with self.assertRaises(ValidationError):
            EvidenceV1(**self._evidence().model_dump(), extra=1)  # type: ignore[call-arg]


class EvidenceSetV1Tests(unittest.TestCase):
    def test_valid_set(self) -> None:
        ev = EvidenceV1(
            evidence_kind="TEXT",
            evidence_id="ev-1",
            source={"source_kind": "KNOWLEDGE_VERSION", "knowledge_version_id": uuid4()},
            parent_chunk_id="parent-id",
            child_chunk_ids=(),
            text="body",
            content_hash=DIGEST,
            token_count=5,
            representation=EvidenceRepresentationV1.FULL,
            scores=ScoreBreakdownV1(),
        )
        es = EvidenceSetV1(
            retrieval_run_id=uuid4(),
            normalized_query="RAG",
            evidences=(ev,),
            sufficiency=EvidenceSufficiencyV1.SUFFICIENT,
            degradation_flags=("RERANK_UNAVAILABLE",),
        )
        self.assertEqual(es.evidences[0].evidence_id, "ev-1")
        self.assertEqual(es.degradation_flags, ("RERANK_UNAVAILABLE",))

    def test_extra_forbidden(self) -> None:
        with self.assertRaises(ValidationError):
            EvidenceSetV1(
                retrieval_run_id=uuid4(),
                normalized_query="q",
                evidences=(),
                sufficiency=EvidenceSufficiencyV1.INSUFFICIENT,
                extra=1,  # type: ignore[call-arg]
            )


if __name__ == "__main__":
    unittest.main()
