"""DEV-04E Bifrost SSE流事件离线契约：稳定事件契约、归一器与终态解析。

不依赖真实Bifrost数据面或PostgreSQL，验证：
- 项目稳定`ModelEventV1`的契约（text_delta必填、sequence从1起、禁额外字段、判别器分流）；
- `BifrostEventV1`的契约（负Token拒绝）；
- `normalize_bifrost_event`的首个Delta发STARTED、chunk_id去重、控制帧记finish_reason、
  Usage记Provider精确值、流开始后错误归一为`MODEL_STREAM_ABORTED`；
- `resolve_stream_terminal`的终态分支（cancelled/error/completed/截断/未开始）与Usage优先级
  （Provider精确值 > 预留最坏估算 > UNKNOWN）。
"""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from knowledge_system.modules.model_gateway.domain.call_state import ModelCallState
from knowledge_system.modules.model_gateway.domain.provider_error import (
    ProviderErrorCode,
    ProviderErrorSource,
    ProviderErrorV1,
)
from knowledge_system.modules.model_gateway.domain.stream_event import (
    MODEL_EVENT_ADAPTER,
    BifrostDeltaEventV1,
    BifrostErrorEventV1,
    BifrostUsageEventV1,
    ModelCompletedEventV1,
    ModelDeltaEventV1,
    ModelErrorEventV1,
    ModelStartedEventV1,
    ModelUsageEventV1,
)
from knowledge_system.modules.model_gateway.domain.stream_normalizer import (
    StreamNormalizerState,
    normalize_bifrost_event,
    resolve_stream_terminal,
)
from knowledge_system.modules.model_gateway.domain.usage import (
    EstimatedModelUsageV1,
    ExactModelUsageV1,
    UnknownModelUsageV1,
)


def make_exact(input_tokens: int, output_tokens: int) -> ExactModelUsageV1:
    return ExactModelUsageV1(
        kind="EXACT", input_tokens=input_tokens, output_tokens=output_tokens, source="PROVIDER"
    )


class ModelEventContractTests(unittest.TestCase):
    def test_delta_requires_text_delta(self) -> None:
        with self.assertRaises(ValidationError):
            ModelDeltaEventV1(type="DELTA", sequence=1)

    def test_sequence_must_start_at_one(self) -> None:
        with self.assertRaises(ValidationError):
            ModelStartedEventV1(type="STARTED", sequence=0)

    def test_extra_field_forbidden(self) -> None:
        with self.assertRaises(ValidationError):
            ModelDeltaEventV1(type="DELTA", sequence=1, text_delta="hi", bogus=1)

    def test_completed_event_valid(self) -> None:
        event = ModelCompletedEventV1(type="COMPLETED", sequence=3, finish_reason="stop")
        self.assertEqual(event.type, "COMPLETED")
        self.assertEqual(event.finish_reason, "stop")

    def test_adapter_discriminates_by_type(self) -> None:
        event = MODEL_EVENT_ADAPTER.validate_python(
            {"type": "DELTA", "sequence": 1, "text_delta": "hi"}
        )
        self.assertIsInstance(event, ModelDeltaEventV1)

    def test_bifrost_usage_rejects_negative_tokens(self) -> None:
        with self.assertRaises(ValidationError):
            BifrostUsageEventV1(type="USAGE", input_tokens=-1, output_tokens=0)


class StreamNormalizerTests(unittest.TestCase):
    def test_first_delta_emits_started_then_delta(self) -> None:
        event = BifrostDeltaEventV1(type="DELTA", chunk_id="c1", text_delta="hi")
        new_state, events = normalize_bifrost_event(event, StreamNormalizerState())
        self.assertEqual(len(events), 2)
        self.assertIsInstance(events[0], ModelStartedEventV1)
        self.assertEqual(events[0].sequence, 1)
        self.assertIsInstance(events[1], ModelDeltaEventV1)
        self.assertEqual(events[1].sequence, 2)
        self.assertEqual(events[1].text_delta, "hi")
        self.assertTrue(new_state.stream_started)

    def test_subsequent_delta_emits_delta_only(self) -> None:
        state = StreamNormalizerState(sequence=2, stream_started=True)
        event = BifrostDeltaEventV1(type="DELTA", chunk_id="c2", text_delta="there")
        new_state, events = normalize_bifrost_event(event, state)
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], ModelDeltaEventV1)
        self.assertEqual(events[0].sequence, 3)

    def test_duplicate_chunk_dropped(self) -> None:
        state = StreamNormalizerState(
            sequence=2, stream_started=True, seen_chunk_ids=frozenset({"c1"})
        )
        event = BifrostDeltaEventV1(type="DELTA", chunk_id="c1", text_delta="hi")
        new_state, events = normalize_bifrost_event(event, state)
        self.assertEqual(events, [])
        self.assertEqual(new_state, state)

    def test_control_delta_records_finish_reason(self) -> None:
        state = StreamNormalizerState(sequence=2, stream_started=True)
        event = BifrostDeltaEventV1(type="DELTA", text_delta=None, finish_reason="stop")
        new_state, events = normalize_bifrost_event(event, state)
        self.assertEqual(events, [])
        self.assertEqual(new_state.finish_reason, "stop")

    def test_usage_event_records_provider_exact(self) -> None:
        state = StreamNormalizerState(sequence=2, stream_started=True)
        event = BifrostUsageEventV1(type="USAGE", input_tokens=10, output_tokens=5)
        new_state, events = normalize_bifrost_event(event, state)
        self.assertEqual(len(events), 1)
        usage_event = events[0]
        self.assertIsInstance(usage_event, ModelUsageEventV1)
        self.assertEqual(usage_event.sequence, 3)
        usage = usage_event.usage
        assert isinstance(usage, ExactModelUsageV1)
        self.assertEqual(usage.input_tokens, 10)
        self.assertEqual(usage.output_tokens, 5)
        self.assertEqual(usage.source, "PROVIDER")
        self.assertEqual(new_state.provider_usage, usage)

    def test_error_before_stream_keeps_http_code(self) -> None:
        event = BifrostErrorEventV1(
            type="ERROR", http_status=500, source=ProviderErrorSource.HTTP_STATUS
        )
        new_state, events = normalize_bifrost_event(event, StreamNormalizerState())
        self.assertEqual(len(events), 1)
        error_event = events[0]
        self.assertIsInstance(error_event, ModelErrorEventV1)
        self.assertEqual(error_event.error.code, ProviderErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertEqual(error_event.error.http_status, 500)
        self.assertEqual(error_event.error.source, ProviderErrorSource.HTTP_STATUS)

    def test_error_after_stream_becomes_model_stream_aborted(self) -> None:
        state = StreamNormalizerState(sequence=2, stream_started=True)
        event = BifrostErrorEventV1(
            type="ERROR", http_status=500, source=ProviderErrorSource.HTTP_STATUS
        )
        new_state, events = normalize_bifrost_event(event, state)
        error_event = events[0]
        self.assertIsInstance(error_event, ModelErrorEventV1)
        self.assertEqual(error_event.error.code, ProviderErrorCode.MODEL_STREAM_ABORTED)
        self.assertEqual(error_event.error.source, ProviderErrorSource.STREAM)
        self.assertIsNone(error_event.error.http_status)


class ResolveStreamTerminalTests(unittest.TestCase):
    def test_completed_with_provider_usage(self) -> None:
        state = StreamNormalizerState(provider_usage=make_exact(10, 5))
        result = resolve_stream_terminal(state=state, completed=True)
        self.assertEqual(result.status, ModelCallState.COMPLETED)
        self.assertIsInstance(result.usage, ExactModelUsageV1)

    def test_completed_with_estimate(self) -> None:
        result = resolve_stream_terminal(
            state=StreamNormalizerState(),
            completed=True,
            estimated_input_tokens=200,
            estimated_output_tokens=100,
        )
        self.assertEqual(result.status, ModelCallState.COMPLETED)
        self.assertIsInstance(result.usage, EstimatedModelUsageV1)

    def test_completed_without_usage_is_unknown(self) -> None:
        result = resolve_stream_terminal(state=StreamNormalizerState(), completed=True)
        self.assertEqual(result.status, ModelCallState.COMPLETED)
        self.assertIsInstance(result.usage, UnknownModelUsageV1)

    def test_provider_usage_wins_over_estimate(self) -> None:
        state = StreamNormalizerState(provider_usage=make_exact(10, 5))
        result = resolve_stream_terminal(
            state=state,
            completed=True,
            estimated_input_tokens=200,
            estimated_output_tokens=100,
        )
        self.assertIsInstance(result.usage, ExactModelUsageV1)

    def test_mid_stream_error_is_failed(self) -> None:
        state = StreamNormalizerState(stream_started=True)
        error = ProviderErrorV1(
            code=ProviderErrorCode.DEPENDENCY_UNAVAILABLE,
            http_status=503,
            source=ProviderErrorSource.HTTP_STATUS,
        )
        result = resolve_stream_terminal(state=state, completed=False, error=error)
        self.assertEqual(result.status, ModelCallState.FAILED)
        self.assertEqual(result.error_code, "MODEL_STREAM_ABORTED")

    def test_pre_stream_http_error_is_failed(self) -> None:
        error = ProviderErrorV1(
            code=ProviderErrorCode.MODEL_ENDPOINT_AUTH_FORBIDDEN,
            http_status=401,
            source=ProviderErrorSource.HTTP_STATUS,
        )
        result = resolve_stream_terminal(
            state=StreamNormalizerState(), completed=False, error=error
        )
        self.assertEqual(result.status, ModelCallState.FAILED)
        self.assertEqual(result.error_code, "MODEL_ENDPOINT_AUTH_FORBIDDEN")

    def test_pre_stream_connection_error_is_unknown(self) -> None:
        error = ProviderErrorV1(
            code=ProviderErrorCode.DEPENDENCY_UNAVAILABLE,
            source=ProviderErrorSource.CONNECTION,
        )
        result = resolve_stream_terminal(
            state=StreamNormalizerState(), completed=False, error=error
        )
        self.assertEqual(result.status, ModelCallState.UNKNOWN)

    def test_cancelled(self) -> None:
        result = resolve_stream_terminal(
            state=StreamNormalizerState(stream_started=True), completed=False, cancelled=True
        )
        self.assertEqual(result.status, ModelCallState.CANCELLED)

    def test_truncated_stream_is_failed(self) -> None:
        state = StreamNormalizerState(stream_started=True)
        result = resolve_stream_terminal(state=state, completed=False)
        self.assertEqual(result.status, ModelCallState.FAILED)
        self.assertEqual(result.error_code, "MODEL_STREAM_ABORTED")

    def test_not_started_without_signal_is_unknown(self) -> None:
        result = resolve_stream_terminal(state=StreamNormalizerState(), completed=False)
        self.assertEqual(result.status, ModelCallState.UNKNOWN)

    def test_full_happy_path_sequences_and_terminal(self) -> None:
        state = StreamNormalizerState()
        state, first = normalize_bifrost_event(
            BifrostDeltaEventV1(type="DELTA", chunk_id="c1", text_delta="hello"), state
        )
        state, second = normalize_bifrost_event(
            BifrostDeltaEventV1(type="DELTA", chunk_id="c2", text_delta=" world"), state
        )
        state, usage_events = normalize_bifrost_event(
            BifrostUsageEventV1(type="USAGE", input_tokens=5, output_tokens=3), state
        )
        events = first + second + usage_events
        self.assertEqual([event.sequence for event in events], [1, 2, 3, 4])
        result = resolve_stream_terminal(state=state, completed=True)
        self.assertEqual(result.status, ModelCallState.COMPLETED)
        self.assertIsInstance(result.usage, ExactModelUsageV1)


if __name__ == "__main__":
    unittest.main()
