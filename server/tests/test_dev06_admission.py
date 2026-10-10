"""DEV-06 Final 离线验收：支持目录投影 + Claim/Code/Artifact 准入 + 输出安全校验。

不接真实模型/Catalog/PG；只证明 `FinalSupportCatalogEntryV1` 目录项契约、零 LLM
`ClaimSupportValidatorV1`/Code 支持/`FinalArtifactSelectionValidatorV1` 三类准入规则与
§5.36 `FinalOutputSecurityValidatorV1` 的文本/URL/Secret 注入检测。
"""

import unittest
from uuid import UUID

from pydantic import ValidationError

from knowledge_system.modules.tasking.domain.core_types import VersionedRefV1
from knowledge_system.modules.tasking.domain.final_admission import (
    FinalSupportCatalogEntryV1,
    claim_support_verdict,
    code_block_support_verdict,
    normalize_claim_text,
    output_is_safe,
    scan_candidate_security,
    scan_output_security,
    select_result_artifacts,
)
from knowledge_system.modules.tasking.domain.final_candidate import (
    FinalArtifactLinkBlockV1,
    FinalClaimDraftV1,
    FinalCodeBlockV1,
    FinalParagraphBlockV1,
    FinalSectionDraftV1,
    FinalSynthesisCandidateV1,
)

ZERO_SHA = "0" * 64
SOURCE_ID = UUID("00000000-0000-4000-8000-0000000000a1")
SNAPSHOT_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
CRITERION_ID = UUID("00000000-0000-4000-8000-000000000001")
CONFLICT_ID = UUID("00000000-0000-4000-8000-000000000002")


def vref(ref_id):
    return VersionedRefV1(ref_id=ref_id, version=1, integrity_digest=ZERO_SHA)


def entry(catalog_id, kind, *, canonical_text=None, verdict=None, artifact_kind=None):
    return FinalSupportCatalogEntryV1(
        catalog_id=catalog_id,
        kind=kind,
        source_ref=vref(SOURCE_ID),
        canonical_text=canonical_text,
        verdict=verdict,
        artifact_kind=artifact_kind,
    )


def claim(
    local_claim_id, claim_kind, text, support_catalog_ids=(), criterion_ids=(), conflict_set_ids=()
):
    return FinalClaimDraftV1(
        local_claim_id=local_claim_id,
        claim_kind=claim_kind,
        text=text,
        support_catalog_ids=support_catalog_ids,
        criterion_ids=criterion_ids,
        conflict_set_ids=conflict_set_ids,
    )


def code_block(code, support_catalog_ids=()):
    return FinalCodeBlockV1(
        block_kind="CODE", language="python", code=code, support_catalog_ids=support_catalog_ids
    )


def artifact_link(catalog_id, display_name="report.pdf"):
    return FinalArtifactLinkBlockV1(
        block_kind="ARTIFACT_LINK", catalog_id=catalog_id, display_name=display_name
    )


def paragraph(claim_local_ids):
    return FinalParagraphBlockV1(block_kind="PARAGRAPH", claim_local_ids=claim_local_ids)


def section(local_section_id, blocks):
    return FinalSectionDraftV1(local_section_id=local_section_id, heading="Section", blocks=blocks)


def candidate(**overrides):
    defaults = dict(
        contract_version="final_synthesis_v1",
        snapshot_id=SNAPSHOT_ID,
        snapshot_digest=ZERO_SHA,
        title="Final answer",
        claims=(),
        sections=(section("s1", (paragraph(("c1",)),)),),
        semantic_criterion_assessments=(),
        gap_reports=(),
        selected_result_artifact_catalog_ids=(),
    )
    defaults.update(overrides)
    return FinalSynthesisCandidateV1(**defaults)


class FinalSupportCatalogEntryTests(unittest.TestCase):
    def test_verified_claim_requires_verdict_and_text(self):
        with self.assertRaises(ValidationError):
            entry("vc1", "VERIFIED_CLAIM", canonical_text="text")
        with self.assertRaises(ValidationError):
            entry("vc1", "VERIFIED_CLAIM", verdict="SUPPORTED")

    def test_verdict_only_for_verified_claim(self):
        with self.assertRaises(ValidationError) as ctx:
            entry("a1", "ADOPTED_ARTIFACT", verdict="SUPPORTED", artifact_kind="REPORT")
        self.assertIn("CATALOG_VERDICT_ONLY_FOR_VERIFIED_CLAIM", str(ctx.exception))

    def test_artifact_kind_only_for_adopted_artifact(self):
        with self.assertRaises(ValidationError) as ctx:
            entry(
                "vc1",
                "VERIFIED_CLAIM",
                canonical_text="t",
                verdict="SUPPORTED",
                artifact_kind="REPORT",
            )
        self.assertIn("CATALOG_ARTIFACT_KIND_ONLY_FOR_ADOPTED_ARTIFACT", str(ctx.exception))

    def test_adopted_artifact_requires_kind(self):
        with self.assertRaises(ValidationError):
            entry("a1", "ADOPTED_ARTIFACT")

    def test_code_snippet_requires_text(self):
        with self.assertRaises(ValidationError):
            entry("cs1", "CODE_SNIPPET")

    def test_all_kinds_accepted(self):
        entry("vc1", "VERIFIED_CLAIM", canonical_text="t", verdict="SUPPORTED")
        entry("e1", "EVIDENCE")
        entry("a1", "ADOPTED_ARTIFACT", artifact_kind="REPORT")
        entry("tf1", "TASK_FACT")
        entry("uf1", "USER_FACT")
        entry("c1", "CONFLICT")
        entry("g1", "GAP")
        entry("cs1", "CODE_SNIPPET", canonical_text="print('hi')")


class NormalizeClaimTextTests(unittest.TestCase):
    def test_crlf_normalized(self):
        self.assertEqual(normalize_claim_text("a\r\nb\rc"), "a\nb\nc")

    def test_nfc_composed(self):
        self.assertEqual(normalize_claim_text("café"), "café")

    def test_whitespace_not_collapsed(self):
        self.assertEqual(normalize_claim_text("a  b"), "a  b")


class ClaimSupportVerdictTests(unittest.TestCase):
    def test_source_fact_supported_exact(self):
        c = claim("c1", "SOURCE_FACT", "项目使用 PostgreSQL。", support_catalog_ids=("vc1",))
        catalog = (
            entry(
                "vc1", "VERIFIED_CLAIM", canonical_text="项目使用 PostgreSQL。", verdict="SUPPORTED"
            ),
        )
        self.assertEqual(claim_support_verdict(c, catalog), "SUPPORTED")

    def test_source_fact_supported_nfc_and_crlf(self):
        c = claim("c1", "SOURCE_FACT", "café\n项目", support_catalog_ids=("vc1",))
        catalog = (
            entry("vc1", "VERIFIED_CLAIM", canonical_text="café\r\n项目", verdict="SUPPORTED"),
        )
        self.assertEqual(claim_support_verdict(c, catalog), "SUPPORTED")

    def test_source_fact_text_drift(self):
        c = claim("c1", "SOURCE_FACT", "项目使用 MySQL。", support_catalog_ids=("vc1",))
        catalog = (
            entry(
                "vc1", "VERIFIED_CLAIM", canonical_text="项目使用 PostgreSQL。", verdict="SUPPORTED"
            ),
        )
        self.assertEqual(claim_support_verdict(c, catalog), "CANONICAL_TEXT_MISMATCH")

    def test_source_fact_no_supported(self):
        c = claim("c1", "SOURCE_FACT", "text", support_catalog_ids=("e1",))
        catalog = (entry("e1", "EVIDENCE"),)
        self.assertEqual(claim_support_verdict(c, catalog), "MISSING_SUPPORTING_CITATION")

    def test_source_fact_partial_only(self):
        c = claim("c1", "SOURCE_FACT", "text", support_catalog_ids=("vc1",))
        catalog = (
            entry("vc1", "VERIFIED_CLAIM", canonical_text="text", verdict="PARTIALLY_SUPPORTED"),
        )
        self.assertEqual(claim_support_verdict(c, catalog), "UNSUPPORTED_FACT_USAGE")

    def test_source_fact_conflicting(self):
        c = claim("c1", "SOURCE_FACT", "text", support_catalog_ids=("vc1",))
        catalog = (entry("vc1", "VERIFIED_CLAIM", canonical_text="text", verdict="CONFLICTING"),)
        self.assertEqual(claim_support_verdict(c, catalog), "CONFLICTING_USED_AS_FACT")

    def test_recommendation_anchored_to_criterion(self):
        c = claim("c1", "RECOMMENDATION", "建议复核。", criterion_ids=(CRITERION_ID,))
        self.assertEqual(claim_support_verdict(c, ()), "SUPPORTED")

    def test_recommendation_anchored_to_conflict(self):
        c = claim("c1", "RECOMMENDATION", "建议复核。", conflict_set_ids=(CONFLICT_ID,))
        self.assertEqual(claim_support_verdict(c, ()), "SUPPORTED")

    def test_recommendation_anchored_to_task_fact(self):
        c = claim("c1", "RECOMMENDATION", "建议复核。", support_catalog_ids=("tf1",))
        catalog = (entry("tf1", "TASK_FACT"),)
        self.assertEqual(claim_support_verdict(c, catalog), "SUPPORTED")

    def test_recommendation_unanchored(self):
        c = claim("c1", "RECOMMENDATION", "建议复核。")
        self.assertEqual(claim_support_verdict(c, ()), "UNANCHORED_NON_FACTUAL_CLAIM")

    def test_limitation_referencing_conflicting(self):
        c = claim(
            "c1",
            "LIMITATION",
            "存在冲突。",
            support_catalog_ids=("vc1",),
            conflict_set_ids=(CONFLICT_ID,),
        )
        catalog = (
            entry("vc1", "VERIFIED_CLAIM", canonical_text="冲突文本", verdict="CONFLICTING"),
        )
        self.assertEqual(claim_support_verdict(c, catalog), "SUPPORTED")

    def test_claim_support_ref_missing(self):
        c = claim("c1", "SOURCE_FACT", "text", support_catalog_ids=("nope",))
        with self.assertRaises(ValueError) as ctx:
            claim_support_verdict(c, ())
        self.assertIn("CLAIM_SUPPORT_REF_MISSING", str(ctx.exception))

    def test_catalog_duplicate_id(self):
        c = claim("c1", "SOURCE_FACT", "text", support_catalog_ids=("vc1",))
        catalog = (
            entry("vc1", "VERIFIED_CLAIM", canonical_text="a", verdict="SUPPORTED"),
            entry("vc1", "VERIFIED_CLAIM", canonical_text="b", verdict="SUPPORTED"),
        )
        with self.assertRaises(ValueError) as ctx:
            claim_support_verdict(c, catalog)
        self.assertIn("CATALOG_DUPLICATE_ID", str(ctx.exception))


class CodeBlockSupportTests(unittest.TestCase):
    def test_code_snippet_exact(self):
        block = code_block("print('hi')", support_catalog_ids=("cs1",))
        catalog = (entry("cs1", "CODE_SNIPPET", canonical_text="print('hi')"),)
        self.assertEqual(code_block_support_verdict(block, catalog), "SUPPORTED")

    def test_code_mismatch(self):
        block = code_block("print('hi')", support_catalog_ids=("cs1",))
        catalog = (entry("cs1", "CODE_SNIPPET", canonical_text="print('bye')"),)
        self.assertEqual(code_block_support_verdict(block, catalog), "UNVERIFIED_CODE")

    def test_no_support(self):
        block = code_block("print('hi')")
        self.assertEqual(code_block_support_verdict(block, ()), "UNVERIFIED_CODE")

    def test_code_ref_missing(self):
        block = code_block("print('hi')", support_catalog_ids=("nope",))
        with self.assertRaises(ValueError) as ctx:
            code_block_support_verdict(block, ())
        self.assertIn("CODE_SUPPORT_REF_MISSING", str(ctx.exception))


class ArtifactSelectionTests(unittest.TestCase):
    def test_bidirectional_complete(self):
        c = candidate(
            sections=(section("s1", (artifact_link("art1"), artifact_link("art2"))),),
            selected_result_artifact_catalog_ids=("art1", "art2"),
        )
        catalog = (
            entry("art1", "ADOPTED_ARTIFACT", artifact_kind="REPORT"),
            entry("art2", "ADOPTED_ARTIFACT", artifact_kind="TABLE"),
        )
        self.assertEqual(select_result_artifacts(c, catalog), ("art1", "art2"))

    def test_link_not_selected(self):
        c = candidate(
            sections=(section("s1", (artifact_link("art1"),)),),
            selected_result_artifact_catalog_ids=(),
        )
        catalog = (entry("art1", "ADOPTED_ARTIFACT", artifact_kind="REPORT"),)
        with self.assertRaises(ValueError) as ctx:
            select_result_artifacts(c, catalog)
        self.assertIn("ARTIFACT_LINK_NOT_SELECTED", str(ctx.exception))

    def test_selected_not_linked(self):
        c = candidate(
            sections=(section("s1", (paragraph(("c1",)),)),),
            selected_result_artifact_catalog_ids=("art1",),
        )
        catalog = (entry("art1", "ADOPTED_ARTIFACT", artifact_kind="REPORT"),)
        with self.assertRaises(ValueError) as ctx:
            select_result_artifacts(c, catalog)
        self.assertIn("SELECTED_ARTIFACT_NOT_LINKED", str(ctx.exception))

    def test_selected_duplicate(self):
        c = candidate(
            sections=(section("s1", (artifact_link("art1"),)),),
            selected_result_artifact_catalog_ids=("art1", "art1"),
        )
        catalog = (entry("art1", "ADOPTED_ARTIFACT", artifact_kind="REPORT"),)
        with self.assertRaises(ValueError) as ctx:
            select_result_artifacts(c, catalog)
        self.assertIn("SELECTED_ARTIFACT_DUPLICATE", str(ctx.exception))

    def test_selected_not_in_catalog(self):
        c = candidate(
            sections=(section("s1", (artifact_link("art1"),)),),
            selected_result_artifact_catalog_ids=("art1",),
        )
        with self.assertRaises(ValueError) as ctx:
            select_result_artifacts(c, ())
        self.assertIn("SELECTED_ARTIFACT_NOT_IN_CATALOG", str(ctx.exception))

    def test_selected_not_adopted(self):
        c = candidate(
            sections=(section("s1", (artifact_link("e1"),)),),
            selected_result_artifact_catalog_ids=("e1",),
        )
        catalog = (entry("e1", "EVIDENCE"),)
        with self.assertRaises(ValueError) as ctx:
            select_result_artifacts(c, catalog)
        self.assertIn("SELECTED_ARTIFACT_NOT_ADOPTED", str(ctx.exception))


class SecurityScanTests(unittest.TestCase):
    def test_clean_text_safe(self):
        self.assertEqual(scan_output_security("正常的项目结论。\n第二行"), ())
        self.assertTrue(output_is_safe("正常文本"))

    def test_script_tag(self):
        self.assertIn("SCRIPT", scan_output_security("<script>alert(1)</script>"))

    def test_html_tag(self):
        self.assertIn("HTML_TAG", scan_output_security("加粗 <b>文本</b>"))

    def test_reserved_marker(self):
        self.assertIn("RESERVED_MARKER", scan_output_security("见 [c:c_001]"))

    def test_path_traversal(self):
        self.assertIn("PATH_TRAVERSAL", scan_output_security("../../etc/passwd"))

    def test_protocol_injection(self):
        self.assertIn("PROTOCOL_INJECTION", scan_output_security("data:text/html,hi"))

    def test_javascript_is_script_and_protocol(self):
        result = scan_output_security("javascript:alert(1)")
        self.assertIn("SCRIPT", result)
        self.assertIn("PROTOCOL_INJECTION", result)

    def test_control_char(self):
        self.assertIn("DANGEROUS_CONTROL_CHAR", scan_output_security("a\x00b"))

    def test_newline_tab_not_dangerous(self):
        self.assertEqual(scan_output_security("a\nb\rc\td"), ())

    def test_secret_sk(self):
        self.assertIn("SECRET", scan_output_security("sk-" + "abcdefgh12345678"))

    def test_secret_private_key(self):
        self.assertIn("SECRET", scan_output_security("-----BEGIN " + "PRIVATE KEY-----"))

    def test_secret_bearer(self):
        self.assertIn("SECRET", scan_output_security("Authorization: Bearer abcdefgh12345678"))

    def test_credentialed_url(self):
        self.assertIn("CREDENTIALED_URL", scan_output_security("https://user:pass@example.com"))

    def test_multiple_violations_deduped_sorted(self):
        result = scan_output_security("<script>javascript:alert(1)</script>", "<script>x</script>")
        self.assertEqual(result, ("PROTOCOL_INJECTION", "SCRIPT"))

    def test_candidate_scan_collects_fields(self):
        c = candidate(
            title="<b>标题</b>",
            claims=(
                claim(
                    "c1", "SOURCE_FACT", "sk-" + "abcdefgh12345678", support_catalog_ids=("vc1",)
                ),
            ),
            sections=(section("s1", (artifact_link("art1", "javascript:bad"),)),),
        )
        result = scan_candidate_security(c)
        self.assertIn("HTML_TAG", result)
        self.assertIn("SECRET", result)
        self.assertIn("SCRIPT", result)


if __name__ == "__main__":
    unittest.main()
