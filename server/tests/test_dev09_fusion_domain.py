"""DEV-09 子批次 5：检索融合与充分性契约的离线盲测（零 LLM/零 embedding/零 I/O）。"""

from __future__ import annotations

import hashlib
import hmac
import unittest
from uuid import uuid4

import rfc8785
from pydantic import ValidationError

from knowledge_system.modules.knowledge.public import (
    CandidateSourceV1,
    CandidateV1,
    EvidenceSufficiencyV1,
    QueryProfileV1,
    SufficiencyPolicyV1,
    SufficiencySignalsV1,
    compute_evidence_id,
    dedup_candidates,
    evaluate_sufficiency,
    rrf,
)

DIGEST = "a" * 64


def _cand(
    chunk_id: str,
    source: CandidateSourceV1,
    *,
    lexical_score: float | None = None,
    vector_score: float | None = None,
    fusion_score: float | None = None,
    rerank_score: float | None = None,
    text_hash: str | None = None,
    parent_chunk_id: str | None = None,
) -> CandidateV1:
    return CandidateV1(
        chunk_id=chunk_id,
        source=source,
        lexical_score=lexical_score,
        vector_score=vector_score,
        fusion_score=fusion_score,
        rerank_score=rerank_score,
        text_hash=text_hash,
        parent_chunk_id=parent_chunk_id,
    )


class CandidateV1Tests(unittest.TestCase):
    def test_sources_exhaustive(self) -> None:
        self.assertEqual(
            {member.value for member in CandidateSourceV1},
            {"IDENTIFIER", "LEXICAL", "VECTOR"},
        )

    def test_string_score_rejected_in_strict_mode(self) -> None:
        with self.assertRaises(ValidationError):
            _cand("c", CandidateSourceV1.LEXICAL, lexical_score="0.8")  # type: ignore[arg-type]

    def test_text_hash_must_be_64_hex(self) -> None:
        with self.assertRaises(ValidationError):
            _cand("c", CandidateSourceV1.LEXICAL, text_hash="not-a-hash")

    def test_extra_forbidden(self) -> None:
        with self.assertRaises(ValidationError):
            CandidateV1(  # type: ignore[call-arg]
                chunk_id="c", source=CandidateSourceV1.LEXICAL, extra=1
            )


class RrfTests(unittest.TestCase):
    def test_single_list_fusion_scores(self) -> None:
        result = rrf(
            [
                [
                    _cand("a", CandidateSourceV1.LEXICAL),
                    _cand("b", CandidateSourceV1.LEXICAL),
                    _cand("c", CandidateSourceV1.LEXICAL),
                ]
            ]
        )
        self.assertEqual([c.chunk_id for c in result], ["a", "b", "c"])
        self.assertAlmostEqual(result[0].fusion_score, 1.0 / 61)
        self.assertAlmostEqual(result[1].fusion_score, 1.0 / 62)
        self.assertAlmostEqual(result[2].fusion_score, 1.0 / 63)

    def test_cross_list_merge_and_scores(self) -> None:
        result = rrf(
            [
                [
                    _cand("a", CandidateSourceV1.LEXICAL, lexical_score=0.9),
                    _cand("shared", CandidateSourceV1.LEXICAL, lexical_score=0.8),
                ],
                [
                    _cand("shared", CandidateSourceV1.VECTOR, vector_score=0.7),
                    _cand("b", CandidateSourceV1.VECTOR, vector_score=0.6),
                ],
            ]
        )
        by_id = {c.chunk_id: c for c in result}
        self.assertAlmostEqual(by_id["shared"].fusion_score, 1.0 / 62 + 1.0 / 61)
        # 首个出现通道为 LEXICAL；原始分各通道保留。
        self.assertEqual(by_id["shared"].source, CandidateSourceV1.LEXICAL)
        self.assertAlmostEqual(by_id["shared"].lexical_score, 0.8)
        self.assertAlmostEqual(by_id["shared"].vector_score, 0.7)

    def test_sorted_descending_stable(self) -> None:
        result = rrf(
            [
                [_cand("x", CandidateSourceV1.LEXICAL), _cand("y", CandidateSourceV1.LEXICAL)],
                [_cand("y", CandidateSourceV1.VECTOR)],
            ]
        )
        self.assertEqual([c.chunk_id for c in result], ["y", "x"])

    def test_k_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            rrf([[_cand("a", CandidateSourceV1.LEXICAL)]], k=0)

    def test_empty_input(self) -> None:
        self.assertEqual(rrf([]), [])

    def test_custom_k(self) -> None:
        result = rrf([[_cand("a", CandidateSourceV1.LEXICAL)]], k=10)
        self.assertAlmostEqual(result[0].fusion_score, 1.0 / 11)


class DedupTests(unittest.TestCase):
    def test_chunk_id_keeps_highest_score(self) -> None:
        candidates = [
            _cand("a", CandidateSourceV1.LEXICAL, fusion_score=0.1),
            _cand("a", CandidateSourceV1.VECTOR, fusion_score=0.9),
        ]
        result = dedup_candidates(candidates)
        self.assertEqual([c.chunk_id for c in result], ["a"])
        self.assertAlmostEqual(result[0].fusion_score, 0.9)

    def test_text_hash_keeps_highest_score(self) -> None:
        candidates = [
            _cand("a", CandidateSourceV1.LEXICAL, fusion_score=0.2, text_hash=DIGEST),
            _cand("b", CandidateSourceV1.VECTOR, fusion_score=0.8, text_hash=DIGEST),
        ]
        result = dedup_candidates(candidates)
        self.assertEqual([c.chunk_id for c in result], ["b"])

    def test_order_preserved_on_tie(self) -> None:
        candidates = [
            _cand("a", CandidateSourceV1.LEXICAL, fusion_score=0.5),
            _cand("b", CandidateSourceV1.VECTOR, fusion_score=0.5),
        ]
        self.assertEqual([c.chunk_id for c in dedup_candidates(candidates)], ["a", "b"])

    def test_distinct_candidates_kept(self) -> None:
        candidates = [
            _cand("a", CandidateSourceV1.LEXICAL, fusion_score=0.5),
            _cand("b", CandidateSourceV1.VECTOR, fusion_score=0.6, text_hash="b" * 64),
        ]
        self.assertEqual([c.chunk_id for c in dedup_candidates(candidates)], ["a", "b"])


class EvidenceIdTests(unittest.TestCase):
    def _payload(self, run_id: str, kv_id: str) -> bytes:
        return rfc8785.dumps(
            {
                "retrieval_run_id": run_id,
                "knowledge_version_id": kv_id,
                "ordered_chunk_ids": ["c1", "c2"],
                "content_hash": DIGEST,
            }
        )

    def test_matches_hmac_sha256(self) -> None:
        run_id = uuid4()
        kv_id = uuid4()
        expected = hmac.new(
            b"server-key", self._payload(str(run_id), str(kv_id)), hashlib.sha256
        ).hexdigest()
        actual = compute_evidence_id(
            server_key=b"server-key",
            retrieval_run_id=run_id,
            knowledge_version_id=kv_id,
            ordered_chunk_ids=("c1", "c2"),
            content_hash=DIGEST,
        )
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 64)

    def test_deterministic(self) -> None:
        run_id = uuid4()
        kv_id = uuid4()
        kwargs = dict(
            server_key=b"k",
            retrieval_run_id=run_id,
            knowledge_version_id=kv_id,
            ordered_chunk_ids=("c1",),
            content_hash=DIGEST,
        )
        self.assertEqual(compute_evidence_id(**kwargs), compute_evidence_id(**kwargs))

    def test_sensitive_to_each_component(self) -> None:
        run_id = uuid4()
        kv_id = uuid4()
        base = compute_evidence_id(
            server_key=b"k",
            retrieval_run_id=run_id,
            knowledge_version_id=kv_id,
            ordered_chunk_ids=("c1",),
            content_hash=DIGEST,
        )
        self.assertNotEqual(
            base,
            compute_evidence_id(
                server_key=b"k",
                retrieval_run_id=run_id,
                knowledge_version_id=kv_id,
                ordered_chunk_ids=("c2",),
                content_hash=DIGEST,
            ),
        )
        self.assertNotEqual(
            base,
            compute_evidence_id(
                server_key=b"k",
                retrieval_run_id=run_id,
                knowledge_version_id=kv_id,
                ordered_chunk_ids=("c1",),
                content_hash="b" * 64,
            ),
        )
        self.assertNotEqual(
            base,
            compute_evidence_id(
                server_key=b"other",
                retrieval_run_id=run_id,
                knowledge_version_id=kv_id,
                ordered_chunk_ids=("c1",),
                content_hash=DIGEST,
            ),
        )

    def test_empty_key_rejected(self) -> None:
        with self.assertRaises(ValueError):
            compute_evidence_id(
                server_key=b"",
                retrieval_run_id=uuid4(),
                knowledge_version_id=uuid4(),
                ordered_chunk_ids=(),
                content_hash=DIGEST,
            )


class SufficiencyTests(unittest.TestCase):
    def _policy(self) -> SufficiencyPolicyV1:
        return SufficiencyPolicyV1(
            min_key_entity_coverage=1.0,
            min_sub_question_coverage=1.0,
            min_top_score=0.8,
        )

    def _ok_signals(self) -> SufficiencySignalsV1:
        return SufficiencySignalsV1(
            result_count=5,
            top_score=0.9,
            required_key_entities=3,
            covered_key_entities=3,
            sub_question_count=2,
            covered_sub_questions=2,
        )

    def _evaluate(self, signals: SufficiencySignalsV1) -> EvidenceSufficiencyV1:
        return evaluate_sufficiency(signals, self._policy())

    def test_conflicting_wins(self) -> None:
        signals = self._ok_signals().model_copy(update={"conflicting": True})
        self.assertEqual(self._evaluate(signals), EvidenceSufficiencyV1.CONFLICTING)

    def test_zero_results_insufficient(self) -> None:
        signals = SufficiencySignalsV1(result_count=0)
        self.assertEqual(self._evaluate(signals), EvidenceSufficiencyV1.INSUFFICIENT)

    def test_sufficient(self) -> None:
        self.assertEqual(self._evaluate(self._ok_signals()), EvidenceSufficiencyV1.SUFFICIENT)

    def test_partial_on_low_score(self) -> None:
        signals = self._ok_signals().model_copy(update={"top_score": 0.5})
        self.assertEqual(self._evaluate(signals), EvidenceSufficiencyV1.PARTIAL)

    def test_partial_on_single_weak_source(self) -> None:
        signals = self._ok_signals().model_copy(update={"single_weak_source": True})
        self.assertEqual(self._evaluate(signals), EvidenceSufficiencyV1.PARTIAL)

    def test_partial_on_degraded(self) -> None:
        signals = self._ok_signals().model_copy(update={"degraded": True})
        self.assertEqual(self._evaluate(signals), EvidenceSufficiencyV1.PARTIAL)

    def test_insufficient_when_all_signals_fail(self) -> None:
        signals = SufficiencySignalsV1(
            result_count=3,
            top_score=0.1,
            required_key_entities=3,
            covered_key_entities=0,
            sub_question_count=2,
            covered_sub_questions=0,
        )
        self.assertEqual(self._evaluate(signals), EvidenceSufficiencyV1.INSUFFICIENT)

    def test_no_key_entities_means_trivially_covered(self) -> None:
        signals = SufficiencySignalsV1(result_count=1, top_score=0.9)
        self.assertEqual(self._evaluate(signals), EvidenceSufficiencyV1.SUFFICIENT)

    def test_coverage_above_required_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SufficiencySignalsV1(required_key_entities=2, covered_key_entities=3)

    def test_sub_question_coverage_above_total_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SufficiencySignalsV1(sub_question_count=1, covered_sub_questions=2)


class QueryProfileTests(unittest.TestCase):
    def test_profiles_exhaustive(self) -> None:
        self.assertEqual(
            {member.value for member in QueryProfileV1},
            {"SINGLE_DOCUMENT", "CROSS_DOCUMENT", "CORPUS_THEME", "RELATIONAL"},
        )


if __name__ == "__main__":
    unittest.main()
