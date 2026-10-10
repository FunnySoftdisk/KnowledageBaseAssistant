"""DEV-04F OTel/Phoenix关联与出网准入离线契约：traceparent编解码、Span白名单、出网准入核对。

不依赖OTel SDK、Collector/Phoenix或真实Bridge/Broker，验证：
- W3C `traceparent` 编解码（仅 version `00`、trace_id/span_id 非全零、flags采样位）与
  `SpanContextV1` 契约；
- `llm_trace_allowlist_v1` 白名单过滤与 Secret 无条件剥离（Secret 优先于白名单）；
- `verify_egress_projection` 的摘要级核对与稳定拒绝码；
- `TelemetryService`/`ModelEgressAdmission` 端口的请求/决策载荷契约（不含 Secret）。
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from pydantic import ValidationError

from knowledge_system.modules.model_gateway.application.egress import (
    EgressAdmissionDecisionV1,
    EgressAdmissionOutcome,
    EgressAdmissionRequestV1,
)
from knowledge_system.modules.model_gateway.domain.egress_admission import (
    EgressBodyDigestV1,
    verify_egress_projection,
)
from knowledge_system.modules.model_gateway.domain.span_policy import (
    LLM_TRACE_ALLOWLIST_V1,
    SpanPolicyViolation,
    filter_llm_span_attributes,
    is_sensitive_attribute_name,
    require_no_secret_attributes,
    strip_secret_attributes,
)
from knowledge_system.modules.model_gateway.domain.trace_context import (
    TRACEPARENT_HEADER,
    TRACEPARENT_VERSION,
    SpanContextV1,
    TraceContextViolation,
    decode_traceparent,
    encode_traceparent,
)

_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_SPAN_ID = "00f067aa0ba902b7"
_DIGEST = "a" * 64


def make_span(sampled: bool = True) -> SpanContextV1:
    return SpanContextV1(trace_id=_TRACE_ID, span_id=_SPAN_ID, sampled=sampled)


def make_body(**overrides: object) -> EgressBodyDigestV1:
    values: dict[str, object] = {
        "model_id": "aliyun/qwen-plus",
        "model_revision": "snapshot-2026-09",
        "parameters_digest": _DIGEST,
        "schema_digest": _DIGEST,
        "input_digest": _DIGEST,
    }
    values.update(overrides)
    return EgressBodyDigestV1(**values)


class TraceContextCodecTests(unittest.TestCase):
    def test_encode_sampled_sets_flag(self) -> None:
        encoded = encode_traceparent(make_span(sampled=True))
        self.assertEqual(encoded, f"00-{_TRACE_ID}-{_SPAN_ID}-01")

    def test_encode_unsampled_sets_flag(self) -> None:
        encoded = encode_traceparent(make_span(sampled=False))
        self.assertEqual(encoded, f"00-{_TRACE_ID}-{_SPAN_ID}-00")

    def test_decode_roundtrip(self) -> None:
        context = decode_traceparent(f"00-{_TRACE_ID}-{_SPAN_ID}-01")
        self.assertEqual(context.trace_id, _TRACE_ID)
        self.assertEqual(context.span_id, _SPAN_ID)
        self.assertTrue(context.sampled)

    def test_decode_unsampled(self) -> None:
        context = decode_traceparent(f"00-{_TRACE_ID}-{_SPAN_ID}-00")
        self.assertFalse(context.sampled)

    def test_decode_rejects_wrong_part_count(self) -> None:
        with self.assertRaises(TraceContextViolation) as ctx:
            decode_traceparent(f"00-{_TRACE_ID}-{_SPAN_ID}")
        self.assertEqual(ctx.exception.code, "TRACE_CONTEXT_INVALID")

    def test_decode_rejects_nonzero_version(self) -> None:
        with self.assertRaises(TraceContextViolation):
            decode_traceparent(f"ff-{_TRACE_ID}-{_SPAN_ID}-01")

    def test_decode_rejects_all_zero_trace_id(self) -> None:
        with self.assertRaises(TraceContextViolation):
            decode_traceparent(f"00-{'0' * 32}-{_SPAN_ID}-01")

    def test_decode_rejects_all_zero_span_id(self) -> None:
        with self.assertRaises(TraceContextViolation):
            decode_traceparent(f"00-{_TRACE_ID}-{'0' * 16}-01")

    def test_decode_rejects_non_hex_trace_id(self) -> None:
        with self.assertRaises(TraceContextViolation):
            decode_traceparent(f"00-{'z' * 32}-{_SPAN_ID}-01")

    def test_decode_rejects_bad_flags(self) -> None:
        with self.assertRaises(TraceContextViolation):
            decode_traceparent(f"00-{_TRACE_ID}-{_SPAN_ID}-0g")

    def test_span_context_requires_hex_span_id(self) -> None:
        with self.assertRaises(ValidationError):
            SpanContextV1(trace_id=_TRACE_ID, span_id="zzzzzzzzzzzzzzzz", sampled=True)

    def test_span_context_rejects_short_trace_id(self) -> None:
        with self.assertRaises(ValidationError):
            SpanContextV1(trace_id="abcd", span_id=_SPAN_ID, sampled=True)

    def test_span_context_forbids_extra_field(self) -> None:
        with self.assertRaises(ValidationError):
            SpanContextV1(trace_id=_TRACE_ID, span_id=_SPAN_ID, bogus=1)

    def test_constants(self) -> None:
        self.assertEqual(TRACEPARENT_HEADER, "traceparent")
        self.assertEqual(TRACEPARENT_VERSION, "00")


class SpanPolicyTests(unittest.TestCase):
    def test_allowlist_is_versioned_and_frozen(self) -> None:
        self.assertIn("approved_model_id", LLM_TRACE_ALLOWLIST_V1)
        self.assertIn("input_tokens", LLM_TRACE_ALLOWLIST_V1)
        self.assertNotIn("authorization", LLM_TRACE_ALLOWLIST_V1)
        self.assertNotIn("input_digest", LLM_TRACE_ALLOWLIST_V1)

    def test_sensitive_name_is_case_insensitive(self) -> None:
        self.assertTrue(is_sensitive_attribute_name("Authorization"))
        self.assertTrue(is_sensitive_attribute_name("  api_key  "))
        self.assertFalse(is_sensitive_attribute_name("input_tokens"))

    def test_strip_secret_attributes_removes_only_secrets(self) -> None:
        attrs = {
            "approved_model_id": "aliyun/qwen-plus",
            "Authorization": "Bearer x",
            "ttft_ms": 42,
        }
        stripped = strip_secret_attributes(attrs)
        self.assertEqual(
            stripped, {"approved_model_id": "aliyun/qwen-plus", "ttft_ms": 42}
        )

    def test_filter_llm_span_attributes_keeps_allowlist(self) -> None:
        attrs = {"approved_model_id": "aliyun/qwen-plus", "noise": 1, "ttft_ms": 42}
        filtered = filter_llm_span_attributes(attrs)
        self.assertEqual(filtered, {"approved_model_id": "aliyun/qwen-plus", "ttft_ms": 42})

    def test_filter_strips_secret_even_if_allowlisted(self) -> None:
        # "error_code" 在白名单内，但若同字典内存在 Secret 属性须无条件剥离。
        attrs = {"error_code": "MODEL_TIMEOUT", "api_key": "sk-123"}
        filtered = filter_llm_span_attributes(attrs)
        self.assertEqual(filtered, {"error_code": "MODEL_TIMEOUT"})

    def test_require_no_secret_attributes_raises(self) -> None:
        with self.assertRaises(SpanPolicyViolation) as ctx:
            require_no_secret_attributes({"Authorization": "Bearer x"})
        self.assertEqual(ctx.exception.code, "SPAN_SECRET_ATTRIBUTE_PRESENT")

    def test_require_no_secret_attributes_passes_clean(self) -> None:
        require_no_secret_attributes({"ttft_ms": 42})


class EgressAdmissionTests(unittest.TestCase):
    def test_body_requires_64_hex_digests(self) -> None:
        with self.assertRaises(ValidationError):
            make_body(parameters_digest="short")

    def test_schema_digest_optional(self) -> None:
        body = EgressBodyDigestV1(
            model_id="m", model_revision="r", parameters_digest=_DIGEST, input_digest=_DIGEST
        )
        self.assertIsNone(body.schema_digest)

    def test_verify_matching_projection_is_none(self) -> None:
        self.assertIsNone(verify_egress_projection(approved=make_body(), actual=make_body()))

    def test_verify_model_id_mismatch(self) -> None:
        reason = verify_egress_projection(
            approved=make_body(), actual=make_body(model_id="aliyun/qwen-max")
        )
        self.assertEqual(reason, "EGRESS_PROJECTION_MISMATCH:MODEL_ID")

    def test_verify_model_revision_mismatch(self) -> None:
        reason = verify_egress_projection(
            approved=make_body(), actual=make_body(model_revision="other-rev")
        )
        self.assertEqual(reason, "EGRESS_PROJECTION_MISMATCH:MODEL_REVISION")

    def test_verify_parameters_mismatch(self) -> None:
        reason = verify_egress_projection(
            approved=make_body(), actual=make_body(parameters_digest="b" * 64)
        )
        self.assertEqual(reason, "EGRESS_PROJECTION_MISMATCH:PARAMETERS")

    def test_verify_schema_mismatch(self) -> None:
        reason = verify_egress_projection(
            approved=make_body(), actual=make_body(schema_digest="b" * 64)
        )
        self.assertEqual(reason, "EGRESS_PROJECTION_MISMATCH:SCHEMA")

    def test_verify_input_mismatch(self) -> None:
        reason = verify_egress_projection(
            approved=make_body(), actual=make_body(input_digest="b" * 64)
        )
        self.assertEqual(reason, "EGRESS_PROJECTION_MISMATCH:INPUT")

    def test_request_contract(self) -> None:
        request = EgressAdmissionRequestV1(
            model_call_id=uuid4(), route_ref="route/aliyun/qwen-plus", body=make_body()
        )
        self.assertIsNotNone(request.model_call_id)

    def test_request_rejects_extra_field(self) -> None:
        with self.assertRaises(ValidationError):
            EgressAdmissionRequestV1(
                model_call_id=uuid4(), route_ref="r", body=make_body(), secret="x"
            )

    def test_decision_outcome_enum(self) -> None:
        self.assertEqual(EgressAdmissionOutcome.ADMITTED.value, "ADMITTED")
        self.assertEqual(EgressAdmissionOutcome.REJECTED.value, "REJECTED")
        self.assertEqual(EgressAdmissionOutcome.RECONCILING.value, "RECONCILING")

    def test_decision_reason_code_optional(self) -> None:
        decision = EgressAdmissionDecisionV1(outcome=EgressAdmissionOutcome.REJECTED)
        self.assertIsNone(decision.reason_code)
        decision = EgressAdmissionDecisionV1(
            outcome=EgressAdmissionOutcome.REJECTED,
            reason_code="EGRESS_PROJECTION_MISMATCH:INPUT",
        )
        self.assertEqual(decision.reason_code, "EGRESS_PROJECTION_MISMATCH:INPUT")


if __name__ == "__main__":
    unittest.main()
