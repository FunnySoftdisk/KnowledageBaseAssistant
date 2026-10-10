"""DEV-06 Final 离线验收：零 LLM `CitationResolverV1` 引用解析。

不接真实模型/Evidence/Catalog；只证明 `CitationSourceV1` 域/Locator 一致校验、六域七 Locator
枚举，以及 `resolve_citations` 按 Claim 首次出现顺序去重 Evidence 分配 c_001..c_200、
需引用 Claim 至少一个 Citation、同一 Evidence 不重复。
"""

import unittest
from uuid import UUID

from pydantic import ValidationError

from knowledge_system.modules.tasking.domain.core_types import VersionedRefV1
from knowledge_system.modules.tasking.domain.final_admission import FinalSupportCatalogEntryV1
from knowledge_system.modules.tasking.domain.final_candidate import FinalClaimDraftV1
from knowledge_system.modules.tasking.domain.final_citation import (
    CitationSourceV1,
    claim_citation_ids,
    resolve_citations,
)

ZERO_SHA = "0" * 64
SOURCE_ID = UUID("00000000-0000-4000-8000-0000000000a1")


def evid(i):
    return UUID(f"00000000-0000-4000-8000-{i:012d}")


def vref(ref_id):
    return VersionedRefV1(ref_id=ref_id, version=1, integrity_digest=ZERO_SHA)


def entry(catalog_id, kind, *, canonical_text=None, verdict=None, evidence_ref=None):
    return FinalSupportCatalogEntryV1(
        catalog_id=catalog_id,
        kind=kind,
        source_ref=vref(SOURCE_ID),
        canonical_text=canonical_text,
        verdict=verdict,
        evidence_ref=evidence_ref,
    )


def claim(local_claim_id, claim_kind, text, support_catalog_ids=()):
    return FinalClaimDraftV1(
        local_claim_id=local_claim_id,
        claim_kind=claim_kind,
        text=text,
        support_catalog_ids=support_catalog_ids,
        criterion_ids=(),
        conflict_set_ids=(),
    )


def cite(
    evidence_id,
    source_domain="EXTERNAL_WEB",
    locator_kind="WEB",
    locator_value="https://example.com",
    preview=None,
):
    return CitationSourceV1(
        evidence_ref_id=evidence_id,
        source_domain=source_domain,
        locator_kind=locator_kind,
        locator_value=locator_value,
        preview=preview,
    )


class CitationSourceTests(unittest.TestCase):
    def test_domain_locator_mismatch_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            cite(evid(1), source_domain="EXTERNAL_WEB", locator_kind="DOI")
        self.assertIn("CITATION_LOCATOR_DOMAIN_MISMATCH", str(ctx.exception))

    def test_scholarly_doi_and_arxiv_ok(self):
        cite(
            evid(1),
            source_domain="EXTERNAL_SCHOLARLY_RECORD",
            locator_kind="DOI",
            locator_value="10.0/example",
        )
        cite(
            evid(2),
            source_domain="EXTERNAL_SCHOLARLY_RECORD",
            locator_kind="ARXIV",
            locator_value="2410.00001",
        )

    def test_internal_domains(self):
        cite(
            evid(3),
            source_domain="INTERNAL_KNOWLEDGE",
            locator_kind="KNOWLEDGE_ENTRY",
            locator_value="entry-key",
        )
        cite(
            evid(4),
            source_domain="TASK_ARTIFACT",
            locator_kind="ARTIFACT_REF",
            locator_value="artifact-key",
        )
        cite(evid(5), source_domain="TASK_FACT", locator_kind="FACT_REF", locator_value="fact-key")

    def test_preview_length_limit(self):
        with self.assertRaises(ValidationError):
            cite(evid(1), preview="x" * 513)


class ResolveCitationsTests(unittest.TestCase):
    def test_first_appearance_order(self):
        c1 = claim("c1", "SOURCE_FACT", "a", support_catalog_ids=("vc1",))
        c2 = claim("c2", "DERIVED_CONCLUSION", "b", support_catalog_ids=("vc2",))
        catalog = (
            entry(
                "vc1",
                "VERIFIED_CLAIM",
                canonical_text="a",
                verdict="SUPPORTED",
                evidence_ref=vref(evid(1)),
            ),
            entry(
                "vc2",
                "VERIFIED_CLAIM",
                canonical_text="b",
                verdict="SUPPORTED",
                evidence_ref=vref(evid(2)),
            ),
        )
        sources = (cite(evid(1)), cite(evid(2)))
        citations = resolve_citations((c1, c2), catalog, sources)
        self.assertEqual([c.citation_id for c in citations], ["c_001", "c_002"])
        self.assertEqual(citations[0].evidence_ref_id, evid(1))
        self.assertEqual(citations[1].evidence_ref_id, evid(2))

    def test_same_evidence_deduped(self):
        c1 = claim("c1", "SOURCE_FACT", "a", support_catalog_ids=("vc1",))
        c2 = claim("c2", "DERIVED_CONCLUSION", "a", support_catalog_ids=("vc1",))
        catalog = (
            entry(
                "vc1",
                "VERIFIED_CLAIM",
                canonical_text="a",
                verdict="SUPPORTED",
                evidence_ref=vref(evid(1)),
            ),
        )
        sources = (cite(evid(1)),)
        citations = resolve_citations((c1, c2), catalog, sources)
        self.assertEqual([c.citation_id for c in citations], ["c_001"])

    def test_non_factual_claims_skipped(self):
        c1 = claim("c1", "RECOMMENDATION", "建议。")
        citations = resolve_citations((c1,), (), ())
        self.assertEqual(citations, ())

    def test_factual_claim_requires_citation(self):
        c1 = claim("c1", "SOURCE_FACT", "a", support_catalog_ids=("vc1",))
        # SUPPORTED VERIFIED_CLAIM 但没有 evidence_ref → 无可引用 Evidence
        catalog = (entry("vc1", "VERIFIED_CLAIM", canonical_text="a", verdict="SUPPORTED"),)
        with self.assertRaises(ValueError) as ctx:
            resolve_citations((c1,), catalog, ())
        self.assertIn("CLAIM_REQUIRES_CITATION", str(ctx.exception))

    def test_citation_source_missing(self):
        c1 = claim("c1", "SOURCE_FACT", "a", support_catalog_ids=("vc1",))
        catalog = (
            entry(
                "vc1",
                "VERIFIED_CLAIM",
                canonical_text="a",
                verdict="SUPPORTED",
                evidence_ref=vref(evid(1)),
            ),
        )
        with self.assertRaises(ValueError) as ctx:
            resolve_citations((c1,), catalog, ())
        self.assertIn("CITATION_SOURCE_MISSING", str(ctx.exception))

    def test_citation_source_duplicate(self):
        c1 = claim("c1", "SOURCE_FACT", "a", support_catalog_ids=("vc1",))
        catalog = (
            entry(
                "vc1",
                "VERIFIED_CLAIM",
                canonical_text="a",
                verdict="SUPPORTED",
                evidence_ref=vref(evid(1)),
            ),
        )
        sources = (cite(evid(1)), cite(evid(1)))
        with self.assertRaises(ValueError) as ctx:
            resolve_citations((c1,), catalog, sources)
        self.assertIn("CITATION_SOURCE_DUPLICATE", str(ctx.exception))

    def test_catalog_duplicate_id(self):
        c1 = claim("c1", "SOURCE_FACT", "a", support_catalog_ids=("vc1",))
        catalog = (
            entry(
                "vc1",
                "VERIFIED_CLAIM",
                canonical_text="a",
                verdict="SUPPORTED",
                evidence_ref=vref(evid(1)),
            ),
            entry(
                "vc1",
                "VERIFIED_CLAIM",
                canonical_text="a",
                verdict="SUPPORTED",
                evidence_ref=vref(evid(1)),
            ),
        )
        with self.assertRaises(ValueError) as ctx:
            resolve_citations((c1,), catalog, ())
        self.assertIn("CATALOG_DUPLICATE_ID", str(ctx.exception))

    def test_citation_limit_exceeded(self):
        claims = []
        catalog = []
        sources = []
        for i in range(1, 202):
            claims.append(claim(f"c{i}", "SOURCE_FACT", f"t{i}", support_catalog_ids=(f"vc{i}",)))
            catalog.append(
                entry(
                    f"vc{i}",
                    "VERIFIED_CLAIM",
                    canonical_text=f"t{i}",
                    verdict="SUPPORTED",
                    evidence_ref=vref(evid(i)),
                )
            )
            sources.append(cite(evid(i)))
        with self.assertRaises(ValueError) as ctx:
            resolve_citations(tuple(claims), tuple(catalog), tuple(sources))
        self.assertIn("CITATION_LIMIT_EXCEEDED", str(ctx.exception))

    def test_claim_citation_ids(self):
        c1 = claim("c1", "SOURCE_FACT", "a", support_catalog_ids=("vc1", "vc2"))
        c2 = claim("c2", "DERIVED_CONCLUSION", "b", support_catalog_ids=("vc2",))
        catalog = (
            entry(
                "vc1",
                "VERIFIED_CLAIM",
                canonical_text="a",
                verdict="SUPPORTED",
                evidence_ref=vref(evid(1)),
            ),
            entry(
                "vc2",
                "VERIFIED_CLAIM",
                canonical_text="b",
                verdict="SUPPORTED",
                evidence_ref=vref(evid(2)),
            ),
        )
        sources = (cite(evid(1)), cite(evid(2)))
        citations = resolve_citations((c1, c2), catalog, sources)
        self.assertEqual(claim_citation_ids(c1, catalog, citations), ("c_001", "c_002"))
        self.assertEqual(claim_citation_ids(c2, catalog, citations), ("c_002",))

    def test_claim_support_ref_missing(self):
        c1 = claim("c1", "SOURCE_FACT", "a", support_catalog_ids=("nope",))
        with self.assertRaises(ValueError) as ctx:
            resolve_citations((c1,), (), ())
        self.assertIn("CLAIM_SUPPORT_REF_MISSING", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
