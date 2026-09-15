import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from core_contract_exports import (
    EXPORT_ROOT,
    PROJECT_ROOT,
    check_exports,
    export_payloads,
)


class CoreExportTests(unittest.TestCase):
    def test_saved_bytes_match_actual_source(self) -> None:
        self.assertEqual(check_exports(EXPORT_ROOT), [])

    def test_real_manifest_hashes_and_no_release_eligibility(self) -> None:
        payloads = export_payloads()
        manifest = json.loads(payloads["manifest.json"])
        self.assertEqual(len(manifest["artifacts"]), 22)
        for item in manifest["artifacts"]:
            body = payloads[item["file"]]
            self.assertEqual(len(body), item["size_bytes"])
            self.assertEqual(hashlib.sha256(body).hexdigest(), item["sha256"])
            self.assertEqual(
                json.loads(body)["$schema"],
                "https://json-schema.org/draft/2020-12/schema",
            )
        for key in (
            "runtime_ready",
            "release_eligible",
            "signature_verified",
            "full_initial_plan_contract_built",
            "compiled_criterion_contract_built",
            "orm_mapping_built",
            "migration_pg_pass",
        ):
            self.assertIs(manifest[key], False)
        self.assertIs(manifest["initial_plan_draft_contract_built"], True)
        self.assertIs(manifest["pure_semantic_checks_built"], True)

    def test_missing_tampered_and_symlink_artifacts_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.assertEqual(len(check_exports(directory)), 23)
            for name, body in export_payloads().items():
                (directory / name).write_bytes(body)
            self.assertEqual(check_exports(directory), [])
            target = directory / "criterion-draft-v1.schema.json"
            target.write_bytes(target.read_bytes() + b" ")
            self.assertEqual(check_exports(directory), [target.name])
            target.unlink()
            target.symlink_to(EXPORT_ROOT / target.name)
            self.assertEqual(check_exports(directory), [target.name])

    def test_field_matrix_exposes_unimplemented_database_boundaries(self) -> None:
        matrix = json.loads(
            (PROJECT_ROOT / "contracts/core/core-field-matrix-v1.json").read_bytes()
        )
        self.assertEqual(matrix["decision_status"], "ACCEPTED")
        self.assertEqual(
            matrix["implementation_stage"], "DTO_SCHEMA_AND_PURE_SEMANTIC_PARTIAL"
        )
        self.assertEqual(matrix["orm_mapping_status"], "NOT_RUN")
        self.assertEqual(matrix["migration_pg_status"], "NOT_RUN")
        task_rules = matrix["column_rules"]["workflow.intelligent_task"]
        self.assertEqual(task_rules["public_initial_status"], "QUEUED")
        self.assertEqual(
            task_rules["required"], ["query_artifact_id", "current_input_id"]
        )
        for forbidden in (
            "knowledge_scope",
            "output_contract",
            "network_policy",
            "immutable_constraints",
        ):
            self.assertIn(forbidden, task_rules["forbidden"])


if __name__ == "__main__":
    unittest.main()
