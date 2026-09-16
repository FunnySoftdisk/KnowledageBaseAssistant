"""DEV-00批准的Output Contract确定性解析测试。"""

import unittest

from knowledge_system.modules.tasking.domain.core_types import SchemaRefV1
from knowledge_system.modules.tasking.domain.input_contracts import OutputContractV1
from knowledge_system.modules.tasking.domain.output_contract_resolver import (
    FixedWorkflowOutputProfileV1,
    OutputContractRegistrySnapshotV1,
    OutputContractResolutionError,
    resolve_output_contract,
)

ZERO_SHA = "0" * 64


def markdown_contract(language: str = "zh-CN") -> OutputContractV1:
    return OutputContractV1(
        format="MARKDOWN",
        delivery="INLINE",
        language=language,
        schema_ref=None,
        template_or_skill_ref=None,
        citations="REQUIRED",
        max_output_bytes=1_048_576,
    )


EMPTY_REGISTRY = OutputContractRegistrySnapshotV1(schema_refs=(), template_or_skill_refs=())
SUPERVISOR = FixedWorkflowOutputProfileV1(decision="SUPERVISOR", default_contract=None)


class OutputContractResolverTests(unittest.TestCase):
    def test_safe_default_and_language_canonicalization(self) -> None:
        result = resolve_output_contract(
            explicit_contract=None,
            authenticated_locale="zh-cn",
            fixed_workflow=SUPERVISOR,
            registry=EMPTY_REGISTRY,
        )
        self.assertEqual(result.contract, markdown_contract())
        self.assertEqual(result.route_decision, "SUPERVISOR")
        self.assertIsNone(result.reason_code)

    def test_fixed_default_only_replaces_absent_explicit_contract(self) -> None:
        fixed = FixedWorkflowOutputProfileV1(
            decision="ADMIT_FIXED_WORKFLOW", default_contract=markdown_contract("en-us")
        )
        admitted = resolve_output_contract(
            explicit_contract=None,
            authenticated_locale="zh-CN",
            fixed_workflow=fixed,
            registry=EMPTY_REGISTRY,
        )
        self.assertEqual(admitted.route_decision, "ADMIT_FIXED_WORKFLOW")
        self.assertEqual(admitted.contract.language, "en-US")

        explicit = markdown_contract("zh-cn").model_copy(update={"citations": "OPTIONAL"})
        mismatch = resolve_output_contract(
            explicit_contract=explicit,
            authenticated_locale="en-US",
            fixed_workflow=fixed,
            registry=EMPTY_REGISTRY,
        )
        self.assertEqual(mismatch.route_decision, "SUPERVISOR")
        self.assertEqual(mismatch.reason_code, "OUTPUT_CONTRACT_MISMATCH")
        self.assertEqual(mismatch.contract.citations, "OPTIONAL")

    def test_exact_explicit_and_fixed_contract_is_admitted(self) -> None:
        contract = markdown_contract("zh-cn")
        result = resolve_output_contract(
            explicit_contract=contract,
            authenticated_locale="en-US",
            fixed_workflow=FixedWorkflowOutputProfileV1(
                decision="ADMIT_FIXED_WORKFLOW", default_contract=markdown_contract("zh-CN")
            ),
            registry=EMPTY_REGISTRY,
        )
        self.assertEqual(result.route_decision, "ADMIT_FIXED_WORKFLOW")
        self.assertEqual(result.contract.language, "zh-CN")

    def test_registry_rejects_missing_and_stale_schema(self) -> None:
        requested = SchemaRefV1(
            schema_id="answer", schema_version="1", schema_digest=ZERO_SHA
        )
        contract = OutputContractV1(
            format="JSON",
            delivery="INLINE",
            language="zh-CN",
            schema_ref=requested,
            template_or_skill_ref=None,
            citations="REQUIRED",
            max_output_bytes=1024,
        )
        with self.assertRaises(OutputContractResolutionError) as missing:
            resolve_output_contract(
                explicit_contract=contract,
                authenticated_locale="zh-CN",
                fixed_workflow=SUPERVISOR,
                registry=EMPTY_REGISTRY,
            )
        self.assertEqual(missing.exception.code, "OUTPUT_CONTRACT_REF_NOT_FOUND")

        stale_registry = OutputContractRegistrySnapshotV1(
            schema_refs=(
                SchemaRefV1(
                    schema_id="answer", schema_version="1", schema_digest="1" * 64
                ),
            ),
            template_or_skill_refs=(),
        )
        with self.assertRaises(OutputContractResolutionError) as stale:
            resolve_output_contract(
                explicit_contract=contract,
                authenticated_locale="zh-CN",
                fixed_workflow=SUPERVISOR,
                registry=stale_registry,
            )
        self.assertEqual(stale.exception.code, "OUTPUT_CONTRACT_REF_STALE")

    def test_invalid_fixed_profile_fails_closed(self) -> None:
        with self.assertRaises(OutputContractResolutionError) as invalid:
            resolve_output_contract(
                explicit_contract=None,
                authenticated_locale="zh-CN",
                fixed_workflow=FixedWorkflowOutputProfileV1(
                    decision="SUPERVISOR", default_contract=markdown_contract()
                ),
                registry=EMPTY_REGISTRY,
            )
        self.assertEqual(invalid.exception.code, "OUTPUT_CONTRACT_PROFILE_INVALID")


if __name__ == "__main__":
    unittest.main()
