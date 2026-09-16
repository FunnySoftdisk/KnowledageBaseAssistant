"""依赖真实Pydantic的契约测试；缺依赖时必须失败，不能静默skip。"""

import unittest

from pydantic import ValidationError

from knowledge_system.modules.model_gateway.application.token_accounting import (
    TOKEN_ACCOUNTING_ADAPTER,
)


class TokenAccountingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.exact = {
            "mode": "EXACT",
            "estimated": False,
            "exact_input_tokens": 120,
            "tokenizer_revision": "fixture-tokenizer-v1",
            "chat_template_hash": "a" * 64,
        }
        self.estimate = {
            "mode": "ESTIMATED_DEV",
            "estimated": True,
            "estimated_input_tokens": 256,
            "estimator_revision": "fixture-byte-estimator-v1",
            "serialized_request_bytes": 128,
            "protocol_overhead_tokens": 128,
        }

    def test_both_closed_modes_validate_and_roundtrip(self) -> None:
        for payload in (self.exact, self.estimate):
            with self.subTest(mode=payload["mode"]):
                value = TOKEN_ACCOUNTING_ADAPTER.validate_python(payload)
                self.assertEqual(TOKEN_ACCOUNTING_ADAPTER.dump_python(value), payload)

    def test_no_cross_mode_fields_or_forged_precision(self) -> None:
        cases = (
            {**self.estimate, "exact_input_tokens": 256},
            {**self.estimate, "tokenizer_revision": "fake"},
            {**self.estimate, "chat_template_hash": "a" * 64},
            {**self.exact, "estimated_input_tokens": 120},
            {**self.exact, "estimated": True},
            {**self.estimate, "estimated": False},
            {**self.exact, "estimated": 0},
            {**self.estimate, "estimated": 1},
        )
        for payload in cases:
            with self.subTest(keys=list(payload)), self.assertRaises(ValidationError):
                TOKEN_ACCOUNTING_ADAPTER.validate_python(payload)

    def test_strict_counts_revisions_and_hashes(self) -> None:
        for count in (-1, True, "120", 120.0):
            with self.subTest(count=count), self.assertRaises(ValidationError):
                TOKEN_ACCOUNTING_ADAPTER.validate_python(
                    {**self.exact, "exact_input_tokens": count}
                )
        for digest in ("a" * 63, "a" * 65, "a" * 64 + "\n", "A" * 64):
            with self.subTest(digest=digest), self.assertRaises(ValidationError):
                TOKEN_ACCOUNTING_ADAPTER.validate_python(
                    {**self.exact, "chat_template_hash": digest}
                )
        for revision in ("", " ", "v1\n"):
            with self.subTest(revision=revision), self.assertRaises(ValidationError):
                TOKEN_ACCOUNTING_ADAPTER.validate_python(
                    {**self.exact, "tokenizer_revision": revision}
                )

    def test_discriminator_and_required_fields(self) -> None:
        for payload in (
            {**self.exact, "mode": "UNKNOWN"},
            {"mode": "EXACT"},
            {},
            {key: value for key, value in self.estimate.items() if key != "mode"},
        ):
            with self.subTest(keys=list(payload)), self.assertRaises(ValidationError):
                TOKEN_ACCOUNTING_ADAPTER.validate_python(payload)

    def test_generated_schema_has_discriminator_and_closed_objects(self) -> None:
        schema = TOKEN_ACCOUNTING_ADAPTER.json_schema()
        self.assertEqual(schema["discriminator"]["propertyName"], "mode")
        self.assertEqual(set(schema["discriminator"]["mapping"]), {"EXACT", "ESTIMATED_DEV"})
        for definition in schema["$defs"].values():
            self.assertFalse(definition["additionalProperties"])
            self.assertIn("estimated", definition["required"])


if __name__ == "__main__":
    unittest.main()
