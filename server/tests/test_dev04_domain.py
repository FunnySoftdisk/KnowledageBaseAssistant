"""DEV-04A模型网关受控调用的领域规则离线验收。

覆盖调用状态机、Provider错误标准化、流前后重试分类与Usage三态账本分支。全部为纯规则
单元测试，不依赖Web/ORM/供应商SDK或真实模型调用。
"""

import unittest

from pydantic import ValidationError

from knowledge_system.modules.model_gateway.domain.call_state import (
    TERMINAL_STATES,
    ModelCallState,
    ModelCallStateViolation,
    can_transition,
    is_terminal,
    require_transition,
)
from knowledge_system.modules.model_gateway.domain.provider_error import (
    ProviderErrorCode,
    ProviderErrorSource,
    disables_endpoint,
    is_transient_error,
    normalize_provider_error,
)
from knowledge_system.modules.model_gateway.domain.retry_policy import (
    RetryDelayDecision,
    RetryOutcome,
    classify_retry,
    compute_next_retry_delay,
    parse_retry_after,
)
from knowledge_system.modules.model_gateway.domain.usage import (
    MODEL_USAGE_ADAPTER,
    SettlementKind,
    resolve_settlement_kind,
)


class CallStateTests(unittest.TestCase):
    def test_terminal_states_closed(self) -> None:
        self.assertEqual(
            TERMINAL_STATES,
            frozenset(
                {
                    ModelCallState.COMPLETED,
                    ModelCallState.FAILED,
                    ModelCallState.CANCELLED,
                    ModelCallState.UNKNOWN,
                }
            ),
        )
        for state in TERMINAL_STATES:
            self.assertTrue(is_terminal(state))

    def test_legal_forward_transitions(self) -> None:
        legal = (
            (ModelCallState.ACCEPTED, ModelCallState.SENDING),
            (ModelCallState.SENDING, ModelCallState.STREAMING),
            (ModelCallState.SENDING, ModelCallState.COMPLETED),
            (ModelCallState.SENDING, ModelCallState.FAILED),
            (ModelCallState.SENDING, ModelCallState.CANCELLED),
            (ModelCallState.SENDING, ModelCallState.UNKNOWN),
            (ModelCallState.STREAMING, ModelCallState.COMPLETED),
            (ModelCallState.STREAMING, ModelCallState.FAILED),
            (ModelCallState.STREAMING, ModelCallState.CANCELLED),
            (ModelCallState.STREAMING, ModelCallState.UNKNOWN),
        )
        for current, target in legal:
            with self.subTest(current=current, target=target):
                self.assertTrue(can_transition(current, target))
                require_transition(current, target)

    def test_illegal_transitions_rejected(self) -> None:
        illegal = (
            (ModelCallState.ACCEPTED, ModelCallState.COMPLETED),
            (ModelCallState.ACCEPTED, ModelCallState.STREAMING),
            (ModelCallState.ACCEPTED, ModelCallState.FAILED),
            (ModelCallState.COMPLETED, ModelCallState.SENDING),
            (ModelCallState.FAILED, ModelCallState.UNKNOWN),
            (ModelCallState.CANCELLED, ModelCallState.COMPLETED),
            (ModelCallState.UNKNOWN, ModelCallState.SENDING),
        )
        for current, target in illegal:
            with self.subTest(current=current, target=target):
                self.assertFalse(can_transition(current, target))
                with self.assertRaises(ModelCallStateViolation) as ctx:
                    require_transition(current, target)
                self.assertEqual(ctx.exception.code, "MODEL_CALL_STATE_TRANSITION_INVALID")


class ProviderErrorTests(unittest.TestCase):
    def test_http_status_normalization(self) -> None:
        mapping = {
            400: ProviderErrorCode.MODEL_REQUEST_INVALID,
            401: ProviderErrorCode.MODEL_ENDPOINT_AUTH_FORBIDDEN,
            403: ProviderErrorCode.MODEL_ENDPOINT_AUTH_FORBIDDEN,
            404: ProviderErrorCode.MODEL_REQUEST_INVALID,
            422: ProviderErrorCode.MODEL_REQUEST_INVALID,
            429: ProviderErrorCode.DEPENDENCY_RATE_LIMITED,
            503: ProviderErrorCode.DEPENDENCY_UNAVAILABLE,
            500: ProviderErrorCode.DEPENDENCY_UNAVAILABLE,
        }
        for status, expected in mapping.items():
            with self.subTest(status=status):
                error = normalize_provider_error(http_status=status)
                self.assertEqual(error.code, expected)
                self.assertEqual(error.http_status, status)
                self.assertIs(error.source, ProviderErrorSource.HTTP_STATUS)

    def test_transport_source_normalization(self) -> None:
        self.assertEqual(
            normalize_provider_error(source=ProviderErrorSource.CONNECTION).code,
            ProviderErrorCode.DEPENDENCY_UNAVAILABLE,
        )
        self.assertEqual(
            normalize_provider_error(source=ProviderErrorSource.TIMEOUT).code,
            ProviderErrorCode.DEPENDENCY_TIMEOUT,
        )
        self.assertEqual(
            normalize_provider_error(source=ProviderErrorSource.STREAM).code,
            ProviderErrorCode.MODEL_STREAM_ABORTED,
        )

    def test_transient_and_endpoint_flags(self) -> None:
        transient = {
            ProviderErrorCode.DEPENDENCY_UNAVAILABLE,
            ProviderErrorCode.DEPENDENCY_RATE_LIMITED,
            ProviderErrorCode.DEPENDENCY_TIMEOUT,
        }
        for code in transient:
            self.assertTrue(is_transient_error(code))
        for code in (
            ProviderErrorCode.MODEL_REQUEST_INVALID,
            ProviderErrorCode.MODEL_ENDPOINT_AUTH_FORBIDDEN,
            ProviderErrorCode.MODEL_STRUCTURED_OUTPUT_INVALID,
        ):
            self.assertFalse(is_transient_error(code))
        self.assertTrue(disables_endpoint(ProviderErrorCode.MODEL_ENDPOINT_AUTH_FORBIDDEN))
        self.assertFalse(disables_endpoint(ProviderErrorCode.DEPENDENCY_UNAVAILABLE))


class RetryPolicyTests(unittest.TestCase):
    def test_stream_started_never_retries(self) -> None:
        for code in (
            ProviderErrorCode.DEPENDENCY_UNAVAILABLE,
            ProviderErrorCode.DEPENDENCY_RATE_LIMITED,
            ProviderErrorCode.DEPENDENCY_TIMEOUT,
        ):
            with self.subTest(code=code):
                self.assertEqual(
                    classify_retry(code, stream_started=True), RetryOutcome.NO_RETRY
                )

    def test_before_stream_only_transient_retries(self) -> None:
        for code in (
            ProviderErrorCode.DEPENDENCY_UNAVAILABLE,
            ProviderErrorCode.DEPENDENCY_RATE_LIMITED,
            ProviderErrorCode.DEPENDENCY_TIMEOUT,
        ):
            with self.subTest(code=code):
                self.assertEqual(
                    classify_retry(code, stream_started=False), RetryOutcome.RETRY
                )
        for code in (
            ProviderErrorCode.MODEL_REQUEST_INVALID,
            ProviderErrorCode.MODEL_ENDPOINT_AUTH_FORBIDDEN,
            ProviderErrorCode.MODEL_STRUCTURED_OUTPUT_INVALID,
        ):
            with self.subTest(code=code):
                self.assertEqual(
                    classify_retry(code, stream_started=False), RetryOutcome.NO_RETRY
                )

    def test_parse_retry_after_delta_seconds(self) -> None:
        for value, expected in (("5", 5), ("0", 0), ("  10 ", 10), ("120", 120)):
            with self.subTest(value=value):
                self.assertEqual(parse_retry_after(value), expected)

    def test_parse_retry_after_invalid(self) -> None:
        for value in (None, "", "abc", "-1", "5.5", "5s"):
            with self.subTest(value=value):
                self.assertIsNone(parse_retry_after(value))

    def test_parse_retry_after_http_date(self) -> None:
        future = parse_retry_after("Wed, 01 Jan 2031 00:00:00 GMT")
        self.assertIsNotNone(future)
        self.assertGreater(future, 0)
        self.assertIsNone(parse_retry_after("Wed, 01 Jan 2020 00:00:00 GMT"))

    def test_exact_retry_after_overrides_backoff_without_jitter(self) -> None:
        self.assertEqual(
            compute_next_retry_delay(attempt=1, activity_id="a", retry_after_seconds=7), 7
        )
        self.assertEqual(
            compute_next_retry_delay(attempt=1, activity_id="a", retry_after_seconds=60), 60
        )

    def test_retry_after_exceeds_budget(self) -> None:
        for retry_after, remaining in ((61, None), (60, 59), (45, 30)):
            with self.subTest(retry_after=retry_after, remaining=remaining):
                self.assertIs(
                    compute_next_retry_delay(
                        attempt=1,
                        activity_id="a",
                        retry_after_seconds=retry_after,
                        remaining_schedule_to_close=remaining,
                    ),
                    RetryDelayDecision.RETRY_AFTER_EXCEEDS_BUDGET,
                )

    def test_invalid_retry_after_falls_back_to_default(self) -> None:
        delay = compute_next_retry_delay(attempt=1, activity_id="a", retry_after_seconds=-1)
        self.assertIsInstance(delay, int)
        self.assertGreaterEqual(delay, 0)

    def test_default_backoff_bounds_and_determinism(self) -> None:
        for attempt, base in ((1, 5.0), (2, 10.0), (3, 20.0), (4, 30.0), (10, 30.0)):
            with self.subTest(attempt=attempt):
                delay = compute_next_retry_delay(attempt=attempt, activity_id="fixed")
                self.assertGreaterEqual(delay, round(base * 0.8))
                self.assertLessEqual(delay, round(base * 1.2))
        first = compute_next_retry_delay(attempt=2, activity_id="fixed")
        second = compute_next_retry_delay(attempt=2, activity_id="fixed")
        self.assertEqual(first, second)

    def test_attempt_must_be_positive_integer(self) -> None:
        for attempt in (0, -1, True, 1.5):
            with self.subTest(attempt=attempt), self.assertRaises(ValueError):
                compute_next_retry_delay(attempt=attempt, activity_id="a")


class UsageTests(unittest.TestCase):
    def test_three_branches_roundtrip(self) -> None:
        exact = {"kind": "EXACT", "input_tokens": 120, "output_tokens": 30, "source": "PROVIDER"}
        estimated = {
            "kind": "ESTIMATED_DEV",
            "estimated_input_tokens": 256,
            "estimated_output_tokens": 64,
            "source": "WORST_CASE_RESERVATION",
        }
        unknown = {"kind": "UNKNOWN"}
        for payload in (exact, estimated, unknown):
            with self.subTest(kind=payload["kind"]):
                value = MODEL_USAGE_ADAPTER.validate_python(payload)
                self.assertEqual(MODEL_USAGE_ADAPTER.dump_python(value), payload)

    def test_no_cross_branch_fields_or_forged_counts(self) -> None:
        cases = (
            {"kind": "UNKNOWN", "input_tokens": 0},
            {"kind": "UNKNOWN", "source": "PROVIDER"},
            {
                "kind": "EXACT",
                "input_tokens": 1,
                "output_tokens": 1,
                "source": "PROVIDER",
                "estimated_input_tokens": 1,
            },
            {
                "kind": "EXACT",
                "input_tokens": 1,
                "output_tokens": 1,
                "source": "WORST_CASE_RESERVATION",
            },
            {
                "kind": "ESTIMATED_DEV",
                "estimated_input_tokens": 1,
                "estimated_output_tokens": 1,
                "source": "WORST_CASE_RESERVATION",
                "input_tokens": 1,
            },
        )
        for payload in cases:
            with self.subTest(keys=list(payload)), self.assertRaises(ValidationError):
                MODEL_USAGE_ADAPTER.validate_python(payload)

    def test_strict_non_negative_counts(self) -> None:
        for count in (-1, True, 1.0):
            with self.subTest(count=count), self.assertRaises(ValidationError):
                MODEL_USAGE_ADAPTER.validate_python(
                    {
                        "kind": "EXACT",
                        "input_tokens": count,
                        "output_tokens": 1,
                        "source": "PROVIDER",
                    }
                )

    def test_discriminator_mapping(self) -> None:
        schema = MODEL_USAGE_ADAPTER.json_schema()
        self.assertEqual(schema["discriminator"]["propertyName"], "kind")
        self.assertEqual(
            set(schema["discriminator"]["mapping"]), {"EXACT", "ESTIMATED_DEV", "UNKNOWN"}
        )

    def test_resolve_settlement_kind(self) -> None:
        self.assertIs(
            resolve_settlement_kind(
                MODEL_USAGE_ADAPTER.validate_python(
                    {"kind": "EXACT", "input_tokens": 1, "output_tokens": 1, "source": "PROVIDER"}
                )
            ),
            SettlementKind.KNOWN,
        )
        self.assertIs(
            resolve_settlement_kind(
                MODEL_USAGE_ADAPTER.validate_python(
                    {
                        "kind": "ESTIMATED_DEV",
                        "estimated_input_tokens": 1,
                        "estimated_output_tokens": 1,
                        "source": "WORST_CASE_RESERVATION",
                    }
                )
            ),
            SettlementKind.WORST_CASE,
        )
        self.assertIs(
            resolve_settlement_kind(MODEL_USAGE_ADAPTER.validate_python({"kind": "UNKNOWN"})),
            SettlementKind.RECONCILING,
        )


if __name__ == "__main__":
    unittest.main()
