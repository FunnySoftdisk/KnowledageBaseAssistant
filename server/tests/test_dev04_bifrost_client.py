"""DEV-04 real Bifrost HTTP/SSE adapter tests without external model calls."""

from __future__ import annotations

import json
import unittest

import httpx
from pydantic import ValidationError

from knowledge_system.infrastructure.model_gateway.bifrost import (
    BifrostClientError,
    BifrostHttpClient,
)
from knowledge_system.modules.model_gateway.domain.chat_request import (
    BifrostChatRequestV1,
    ChatJsonSchemaV1,
    ChatMessageV1,
    ChatResponseFormatV1,
)
from knowledge_system.modules.model_gateway.domain.provider_error import ProviderErrorSource
from knowledge_system.modules.model_gateway.domain.stream_event import (
    BifrostDeltaEventV1,
    BifrostErrorEventV1,
    BifrostUsageEventV1,
)


def _request() -> BifrostChatRequestV1:
    return BifrostChatRequestV1(
        model="bailian-bridge/qwen-test",
        messages=(ChatMessageV1(role="user", content="public test input"),),
        temperature=0.0,
        top_p=1.0,
        max_tokens=128,
        response_format=ChatResponseFormatV1(
            json_schema=ChatJsonSchemaV1(
                name="trial_result",
                schema={
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
            )
        ),
    )


class BifrostChatRequestTests(unittest.TestCase):
    def test_contract_has_no_url_key_retry_or_fallback_fields(self) -> None:
        dumped = _request().model_dump(by_alias=True)
        self.assertNotIn("base_url", dumped)
        self.assertNotIn("api_key", dumped)
        self.assertNotIn("max_retries", dumped)
        self.assertNotIn("fallback", dumped)
        self.assertEqual(dumped["response_format"]["json_schema"]["schema"]["type"], "object")

    def test_model_must_include_approved_provider_prefix(self) -> None:
        values = _request().model_dump()
        values["model"] = "qwen-test"
        with self.assertRaises(ValidationError):
            BifrostChatRequestV1.model_validate(values)


class BifrostHttpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_parses_delta_finish_and_usage(self) -> None:
        captured: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            body = (
                'data: {"id":"call-1","choices":[{"delta":{"content":"hello"},'
                '"finish_reason":null}]}\n\n'
                'data: {"id":"call-1","choices":[{"delta":{},'
                '"finish_reason":"stop"}]}\n\n'
                'data: {"id":"call-1","choices":[],"usage":'
                '{"prompt_tokens":8,"completion_tokens":2}}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

        client = BifrostHttpClient(
            base_url="https://bifrost.internal:8080",
            transport=httpx.MockTransport(handle),
        )
        events = [
            event
            async for event in client.stream(
                model_call_id="model-call-1",
                trace_id="a" * 32,
                route_ref="route/planning-strong/v1",
                request=_request(),
            )
        ]

        self.assertEqual(len(events), 3)
        self.assertIsInstance(events[0], BifrostDeltaEventV1)
        self.assertEqual(events[0].text_delta, "hello")
        self.assertIsInstance(events[1], BifrostDeltaEventV1)
        self.assertEqual(events[1].finish_reason, "stop")
        self.assertIsInstance(events[2], BifrostUsageEventV1)
        self.assertEqual(events[2].input_tokens, 8)
        self.assertEqual(events[2].output_tokens, 2)
        self.assertEqual(captured[0].url.path, "/v1/chat/completions")
        self.assertEqual(captured[0].headers["x-bf-eh-x-knowledge-model-call-id"], "model-call-1")
        sent = json.loads(captured[0].content)
        self.assertTrue(sent["stream"])
        self.assertTrue(sent["stream_options"]["include_usage"])

    async def test_http_error_is_bounded_event_without_response_body(self) -> None:
        client = BifrostHttpClient(
            base_url="https://bifrost.internal:8080",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(503, text="sensitive upstream body")
            ),
        )
        events = [
            event
            async for event in client.stream(
                model_call_id="model-call-2",
                trace_id="b" * 32,
                route_ref="route/planning-strong/v1",
                request=_request(),
            )
        ]
        self.assertEqual(events, [BifrostErrorEventV1(type="ERROR", http_status=503)])
        self.assertNotIn("sensitive", repr(events))

    async def test_transport_timeout_is_normalized(self) -> None:
        def timeout(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("must not escape")

        client = BifrostHttpClient(
            base_url="https://bifrost.internal:8080",
            transport=httpx.MockTransport(timeout),
        )
        events = [
            event
            async for event in client.stream(
                model_call_id="model-call-3",
                trace_id="c" * 32,
                route_ref="route/planning-strong/v1",
                request=_request(),
            )
        ]
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], BifrostErrorEventV1)
        self.assertEqual(events[0].source, ProviderErrorSource.TIMEOUT)

    def test_mtls_requires_ca_file_path_when_cert_is_provided(self) -> None:
        with self.assertRaises(BifrostClientError) as caught:
            BifrostHttpClient(
                base_url="https://bifrost.internal:8080",
                verify=True,
                cert=("/missing/tls.crt", "/missing/tls.key"),
            )
        self.assertEqual(caught.exception.args[0], "BIFROST_MTLS_CA_FILE_REQUIRED")

    def test_mtls_material_is_loaded_into_ssl_context(self) -> None:
        with self.assertRaises(BifrostClientError) as caught:
            BifrostHttpClient(
                base_url="https://bifrost.internal:8080",
                verify="/missing/ca.crt",
                cert=("/missing/tls.crt", "/missing/tls.key"),
            )
        self.assertEqual(caught.exception.args[0], "BIFROST_MTLS_MATERIAL_INVALID")

    def test_base_url_requires_https_origin_without_path(self) -> None:
        for value in (
            "http://bifrost.internal:8080",
            "https://bifrost.internal:8080/admin",
            "https://bifrost.internal:8080?x=1",
        ):
            with self.subTest(value=value), self.assertRaises(BifrostClientError):
                BifrostHttpClient(base_url=value)


if __name__ == "__main__":
    unittest.main()
