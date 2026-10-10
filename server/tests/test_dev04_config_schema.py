"""DEV-04D 不可变配置投影与严格结构化输出的离线契约。

验证`StrictOutputSchemaV1`/`ConfigProjectionV1`的冻结/严格/禁止额外字段不变式，`is_strict_json_schema`
对封闭/嵌套/可空对象的严格判定，`resolve_output_mode`/`require_strict_output`对“不支持不得静默退
json_object”的闭合，以及配置投影“来源批准且Digest核对才启用”与“调用方不得覆盖base_url/模型/凭据/
回退”的稳定拒绝。不依赖真实PostgreSQL、不接真实模型。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from knowledge_system.modules.model_gateway.domain.config_projection import (
    CallerOverrideV1,
    ConfigProjectionStatus,
    ConfigProjectionV1,
    ConfigProjectionViolation,
    EndpointProjectionV1,
    ModelBindingProjectionV1,
    RouteProjectionV1,
    assert_no_caller_override,
    resolve_config_enablement,
)
from knowledge_system.modules.model_gateway.domain.output_schema import (
    OutputSchemaMode,
    StrictOutputSchemaV1,
    StructuredOutputViolation,
    is_strict_json_schema,
    require_strict_output,
    resolve_output_mode,
)

DIGEST = "a" * 64


def make_schema() -> StrictOutputSchemaV1:
    return StrictOutputSchemaV1(
        output_schema_id="judge_verdict_v1",
        output_schema_version="1",
        schema_digest=DIGEST,
    )


def make_projection(**overrides) -> ConfigProjectionV1:
    endpoint = EndpointProjectionV1(
        endpoint_id=uuid4(),
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider="bailian",
        capability_aliases=("planning_strong",),
        secret_ref="openbao://kv/project/bailian-ref",
        data_boundary="PUBLIC_DEVELOPMENT",
        timeout_ms=60_000,
    )
    route = RouteProjectionV1(
        capability_alias="planning_strong", endpoint_ids=(endpoint.endpoint_id,)
    )
    binding = ModelBindingProjectionV1(
        capability_alias="planning_strong",
        model_id="qwen3.8-max-0902",
        model_revision="approved-rev",
        endpoint_id=endpoint.endpoint_id,
    )
    return ConfigProjectionV1(
        projection_id=overrides.pop("projection_id", uuid4()),
        projection_digest=DIGEST,
        source_kind="BINDING",
        source_ref="planning_strong@binding-v1",
        endpoints=(endpoint,),
        routes=(route,),
        model_bindings=(binding,),
        rate_card_version="rate-card-v1",
        frozen_at=datetime.now(UTC),
        **overrides,
    )


class StrictJsonSchemaTests(unittest.TestCase):
    def test_closed_object_is_strict(self) -> None:
        self.assertTrue(
            is_strict_json_schema(
                {
                    "type": "object",
                    "properties": {"verdict": {"type": "string"}},
                    "additionalProperties": False,
                    "required": ["verdict"],
                }
            )
        )

    def test_open_object_without_additional_properties_rejected(self) -> None:
        self.assertFalse(
            is_strict_json_schema({"type": "object", "properties": {"a": {"type": "string"}}})
        )

    def test_explicit_true_rejected(self) -> None:
        self.assertFalse(
            is_strict_json_schema(
                {"type": "object", "additionalProperties": True}
            )
        )

    def test_nested_closed_object_is_strict(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "item": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}},
                    "additionalProperties": False,
                }
            },
            "additionalProperties": False,
        }
        self.assertTrue(is_strict_json_schema(schema))

    def test_nested_open_object_rejected(self) -> None:
        schema = {
            "type": "object",
            "properties": {"item": {"type": "object", "properties": {"x": {"type": "integer"}}}},
            "additionalProperties": False,
        }
        self.assertFalse(is_strict_json_schema(schema))

    def test_nullable_object_via_any_of_allowed(self) -> None:
        schema = {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {"v": {"type": "string"}},
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        }
        self.assertTrue(is_strict_json_schema(schema))

    def test_array_items_closed_required(self) -> None:
        self.assertTrue(
            is_strict_json_schema(
                {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": False},
                }
            )
        )
        self.assertFalse(
            is_strict_json_schema(
                {
                    "type": "array",
                    "items": {"type": "object", "properties": {"a": {"type": "string"}}},
                }
            )
        )

    def test_top_level_must_be_dict(self) -> None:
        self.assertFalse(is_strict_json_schema([]))
        self.assertFalse(is_strict_json_schema(None))
        self.assertFalse(is_strict_json_schema("not a schema"))

    def test_object_free_schema_is_vacuously_strict(self) -> None:
        self.assertTrue(is_strict_json_schema({"type": "string"}))


class OutputModeTests(unittest.TestCase):
    def test_no_schema_yields_json_object(self) -> None:
        self.assertEqual(
            resolve_output_mode(schema=None, provider_supports_strict_schema=False),
            OutputSchemaMode.JSON_OBJECT,
        )

    def test_supported_provider_yields_strict(self) -> None:
        self.assertEqual(
            resolve_output_mode(schema=make_schema(), provider_supports_strict_schema=True),
            OutputSchemaMode.STRICT_JSON_SCHEMA,
        )

    def test_unsupported_provider_yields_unsupported(self) -> None:
        self.assertEqual(
            resolve_output_mode(schema=make_schema(), provider_supports_strict_schema=False),
            OutputSchemaMode.UNSUPPORTED,
        )

    def test_require_strict_output_raises_when_unsupported(self) -> None:
        with self.assertRaises(StructuredOutputViolation) as raised:
            require_strict_output(schema=make_schema(), provider_supports_strict_schema=False)
        self.assertEqual(raised.exception.code, "STRUCTURED_OUTPUT_SCHEMA_UNSUPPORTED")

    def test_require_strict_output_passes_when_supported_or_no_schema(self) -> None:
        require_strict_output(schema=make_schema(), provider_supports_strict_schema=True)
        require_strict_output(schema=None, provider_supports_strict_schema=False)


class ConfigProjectionRoundtripTests(unittest.TestCase):
    def test_projection_frozen_strict(self) -> None:
        projection = make_projection()
        with self.assertRaises(ValidationError):
            projection.endpoints = ()  # type: ignore[misc]

    def test_projection_rejects_unknown_field(self) -> None:
        with self.assertRaises(ValidationError):
            make_projection(unknown_field="x")  # type: ignore[call-arg]

    def test_endpoint_does_not_hold_plaintext_secret(self) -> None:
        # secret_ref 只是引用；EndpointProjectionV1 没有明文凭据字段。
        endpoint = make_projection().endpoints[0]
        self.assertNotIn("credential", endpoint.model_dump())


class ConfigEnablementTests(unittest.TestCase):
    def test_enabled_only_when_approved_and_digest_verified(self) -> None:
        self.assertEqual(
            resolve_config_enablement(source_approved=True, digest_verified=True),
            ConfigProjectionStatus.ENABLED,
        )
        self.assertEqual(
            resolve_config_enablement(source_approved=False, digest_verified=True),
            ConfigProjectionStatus.DISABLED,
        )
        self.assertEqual(
            resolve_config_enablement(source_approved=True, digest_verified=False),
            ConfigProjectionStatus.DISABLED,
        )
        self.assertEqual(
            resolve_config_enablement(source_approved=False, digest_verified=False),
            ConfigProjectionStatus.DISABLED,
        )


class CallerOverrideTests(unittest.TestCase):
    def test_none_and_empty_override_allowed(self) -> None:
        assert_no_caller_override(None)
        assert_no_caller_override(CallerOverrideV1())

    def test_base_url_override_rejected(self) -> None:
        with self.assertRaises(ConfigProjectionViolation) as raised:
            assert_no_caller_override(CallerOverrideV1(base_url="https://evil.example"))
        self.assertEqual(raised.exception.code, "CONFIG_OVERRIDE_FORBIDDEN")

    def test_model_override_rejected(self) -> None:
        with self.assertRaises(ConfigProjectionViolation):
            assert_no_caller_override(CallerOverrideV1(model_id="qwen-max"))
        with self.assertRaises(ConfigProjectionViolation):
            assert_no_caller_override(CallerOverrideV1(model_revision="other"))

    def test_credential_override_rejected(self) -> None:
        with self.assertRaises(ConfigProjectionViolation):
            assert_no_caller_override(CallerOverrideV1(credential="sk-live-..."))

    def test_fallback_rejected(self) -> None:
        with self.assertRaises(ConfigProjectionViolation):
            assert_no_caller_override(CallerOverrideV1(fallback=True))

    def test_unknown_override_key_rejected_by_schema(self) -> None:
        with self.assertRaises(ValidationError):
            CallerOverrideV1(extra_route="x")  # type: ignore[call-arg]


if __name__ == "__main__":
    unittest.main()
