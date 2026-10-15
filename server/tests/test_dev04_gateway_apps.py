"""Private model-gateway process HTTP surface tests."""

from __future__ import annotations

import unittest
from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from knowledge_system.infrastructure.model_gateway.apps import (
    create_admission_app,
    create_bridge_app,
    create_secret_broker_app,
)
from knowledge_system.infrastructure.model_gateway.bridge import ModelEgressBridgeError
from knowledge_system.infrastructure.model_gateway.openbao import OpenBaoSecretError
from knowledge_system.modules.model_gateway.application.egress import (
    EgressAdmissionDecisionV1,
    EgressAdmissionOutcome,
    EgressAdmissionRequestV1,
)
from knowledge_system.modules.model_gateway.application.secrets import (
    ProviderSecretReadRequestV1,
)
from knowledge_system.modules.model_gateway.domain.chat_digest import ProviderChatRequestV1

MODEL_CALL_ID = UUID("00000000-0000-0000-0000-000000000001")
TRACE_ID = "1" * 32
SECRET_REF = "openbao-kv-v2://knowledge-model/model-provider/" + "a" * 64


def _chat_body() -> dict[str, object]:
    return {
        "model": "qwen-plus",
        "messages": [{"role": "user", "content": "public synthetic input"}],
        "temperature": 0.0,
        "top_p": 1.0,
        "max_tokens": 128,
        "stream": True,
        "stream_options": {"include_usage": True},
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "goal_plan",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"goal": {"type": "string"}},
                    "required": ["goal"],
                    "additionalProperties": False,
                },
            },
        },
    }


def _admission_body() -> dict[str, object]:
    return {
        "model_call_id": str(MODEL_CALL_ID),
        "route_ref": "route/planning-strong/v1",
        "body": {
            "model_id": "qwen-plus",
            "model_revision": "qwen-plus",
            "parameters_digest": "a" * 64,
            "schema_digest": "b" * 64,
            "input_digest": "c" * 64,
        },
    }


class _Authority:
    def __init__(self) -> None:
        self.requests: list[EgressAdmissionRequestV1] = []

    async def admit(self, *, request: EgressAdmissionRequestV1) -> EgressAdmissionDecisionV1:
        self.requests.append(request)
        return EgressAdmissionDecisionV1(
            outcome=EgressAdmissionOutcome.REJECTED,
            reason_code="EGRESS_NOT_READY",
        )


class _Broker:
    def __init__(self, *, error: OpenBaoSecretError | None = None) -> None:
        self.error = error
        self.issued: list[bytearray] = []

    async def read_provider_key(self, request: ProviderSecretReadRequestV1) -> bytearray:
        del request
        if self.error is not None:
            raise self.error
        value = bytearray(b"synthetic-provider-key")
        self.issued.append(value)
        return value


class _Bridge:
    def __init__(self, *, error: ModelEgressBridgeError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[UUID, str, ProviderChatRequestV1]] = []
        self.closed = False

    async def stream(
        self,
        *,
        model_call_id: UUID,
        route_ref: str,
        request: ProviderChatRequestV1,
    ) -> AsyncGenerator[bytes, None]:
        try:
            self.calls.append((model_call_id, route_ref, request))
            if self.error is not None:
                raise self.error
            yield b'data: {"choices":[]}\n\n'
            yield b"data: [DONE]\n\n"
        finally:
            self.closed = True


class GatewayAppTests(unittest.TestCase):
    def test_admission_app_passes_a_strict_request_to_the_authority(self) -> None:
        authority = _Authority()
        with TestClient(create_admission_app(authority)) as client:
            response = client.post("/internal/v1/model-egress/admit", json=_admission_body())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["outcome"], "REJECTED")
        self.assertEqual(authority.requests[0].model_call_id, MODEL_CALL_ID)

    def test_secret_broker_returns_octets_then_zeroizes_source_buffer(self) -> None:
        broker = _Broker()
        with TestClient(create_secret_broker_app(broker)) as client:
            response = client.post(
                "/internal/v1/provider-secrets/read",
                json={
                    "model_call_id": str(MODEL_CALL_ID),
                    "route_ref": "route/planning-strong/v1",
                    "secret_ref": SECRET_REF,
                    "secret_version": 1,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"synthetic-provider-key")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(broker.issued[0], bytearray(len(b"synthetic-provider-key")))

    def test_secret_broker_failure_has_only_a_stable_code(self) -> None:
        broker = _Broker(error=OpenBaoSecretError("PROVIDER_SECRET_READ_NOT_AUTHORIZED"))
        with TestClient(create_secret_broker_app(broker)) as client:
            response = client.post(
                "/internal/v1/provider-secrets/read",
                json={
                    "model_call_id": str(uuid4()),
                    "route_ref": "route/planning-strong/v1",
                    "secret_ref": SECRET_REF,
                    "secret_version": 1,
                },
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "PROVIDER_SECRET_READ_NOT_AUTHORIZED")

    def test_bridge_primes_and_streams_the_upstream_generator(self) -> None:
        bridge = _Bridge()
        with TestClient(create_bridge_app(bridge)) as client:  # type: ignore[arg-type]
            response = client.post(
                "/v1/chat/completions",
                headers={
                    "x-knowledge-model-call-id": str(MODEL_CALL_ID),
                    "x-knowledge-route-ref": "route/planning-strong/v1",
                    "x-knowledge-trace-id": TRACE_ID,
                },
                json=_chat_body(),
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"[DONE]", response.content)
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertEqual(bridge.calls[0][0], MODEL_CALL_ID)
        self.assertTrue(bridge.closed)

    def test_bridge_rejects_before_response_headers_and_closes_stream(self) -> None:
        bridge = _Bridge(error=ModelEgressBridgeError("MODEL_EGRESS_NOT_ADMITTED"))
        with TestClient(create_bridge_app(bridge)) as client:  # type: ignore[arg-type]
            response = client.post(
                "/v1/chat/completions",
                headers={
                    "x-knowledge-model-call-id": str(MODEL_CALL_ID),
                    "x-knowledge-route-ref": "route/planning-strong/v1",
                    "x-knowledge-trace-id": TRACE_ID,
                },
                json=_chat_body(),
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "MODEL_EGRESS_NOT_ADMITTED")
        self.assertTrue(bridge.closed)

    def test_bridge_rejects_bad_correlation_headers_without_calling_bridge(self) -> None:
        bridge = _Bridge()
        with TestClient(create_bridge_app(bridge)) as client:  # type: ignore[arg-type]
            response = client.post(
                "/v1/chat/completions",
                headers={
                    "x-knowledge-model-call-id": "not-a-uuid",
                    "x-knowledge-route-ref": "route/planning-strong/v1",
                    "x-knowledge-trace-id": "0" * 32,
                },
                json=_chat_body(),
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "MODEL_CALL_ID_INVALID")
        self.assertEqual(bridge.calls, [])


if __name__ == "__main__":
    unittest.main()
