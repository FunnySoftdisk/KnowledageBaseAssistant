"""Bridge-side mTLS client tests without sockets or real credentials."""

from __future__ import annotations

import ssl
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx

from knowledge_system.infrastructure.model_gateway.internal_http import (
    HttpModelEgressAdmission,
    HttpProviderSecretBroker,
    InternalModelGatewayHttpError,
    build_workload_mtls_context,
)
from knowledge_system.modules.model_gateway.application.egress import (
    EgressAdmissionOutcome,
    EgressAdmissionRequestV1,
)
from knowledge_system.modules.model_gateway.application.secrets import (
    ProviderSecretReadRequestV1,
)
from knowledge_system.modules.model_gateway.domain.egress_admission import EgressBodyDigestV1

SECRET_REF = "openbao-kv-v2://knowledge-model/model-provider/" + "a" * 64


def _context() -> ssl.SSLContext:
    return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _admission_request() -> EgressAdmissionRequestV1:
    return EgressAdmissionRequestV1(
        model_call_id=uuid4(),
        route_ref="route/planning-strong/v1",
        body=EgressBodyDigestV1(
            model_id="qwen-plus",
            model_revision="qwen-plus",
            parameters_digest="a" * 64,
            schema_digest="b" * 64,
            input_digest="c" * 64,
        ),
    )


def _secret_request() -> ProviderSecretReadRequestV1:
    return ProviderSecretReadRequestV1(
        model_call_id=uuid4(),
        route_ref="route/planning-strong/v1",
        secret_ref=SECRET_REF,
        secret_version=1,
    )


class InternalHttpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_admission_accepts_only_a_strict_valid_decision(self) -> None:
        captured: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "outcome": "ADMITTED",
                    "reason_code": None,
                    "permit": {
                        "provider": "alibaba_bailian",
                        "provider_model": "qwen-plus",
                        "secret_ref": SECRET_REF,
                        "secret_version": 1,
                    },
                },
            )

        client = HttpModelEgressAdmission(
            base_url="https://model-gateway-control:8443",
            ssl_context=_context(),
            transport=httpx.MockTransport(handle),
        )
        decision = await client.admit(request=_admission_request())

        self.assertEqual(decision.outcome, EgressAdmissionOutcome.ADMITTED)
        self.assertEqual(captured[0].url.path, "/internal/v1/model-egress/admit")
        self.assertNotIn("api_key", captured[0].content.decode())

    async def test_admission_transport_and_payload_fail_closed(self) -> None:
        cases = (
            (
                httpx.Response(503, text="sensitive upstream body"),
                "EGRESS_ADMISSION_SERVICE_REJECTED",
            ),
            (
                httpx.Response(200, json={"outcome": "ADMITTED"}),
                "EGRESS_ADMISSION_RESPONSE_INVALID",
            ),
        )
        for response, expected in cases:
            with self.subTest(expected=expected):
                client = HttpModelEgressAdmission(
                    base_url="https://model-gateway-control:8443",
                    ssl_context=_context(),
                    transport=httpx.MockTransport(lambda _, value=response: value),
                )
                decision = await client.admit(request=_admission_request())
                self.assertEqual(decision.outcome, EgressAdmissionOutcome.RECONCILING)
                self.assertEqual(decision.reason_code, expected)
                self.assertNotIn("sensitive", decision.model_dump_json())

    async def test_broker_returns_a_mutable_secret_without_echoing_it_in_errors(self) -> None:
        secret = b"synthetic-provider-key"
        client = HttpProviderSecretBroker(
            base_url="https://secret-broker:8443",
            ssl_context=_context(),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"content-type": "application/octet-stream"},
                    content=secret,
                )
            ),
        )
        self.assertEqual(await client.read_provider_key(_secret_request()), bytearray(secret))

        rejected = HttpProviderSecretBroker(
            base_url="https://secret-broker:8443",
            ssl_context=_context(),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(403, text="synthetic-provider-key")
            ),
        )
        with self.assertRaisesRegex(
            InternalModelGatewayHttpError, "SECRET_BROKER_READ_REJECTED"
        ) as raised:
            await rejected.read_provider_key(_secret_request())
        self.assertNotIn("synthetic-provider-key", str(raised.exception))

    async def test_broker_rejects_non_octet_stream_and_invalid_secret_bytes(self) -> None:
        for response in (
            httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}"),
            httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=b"contains whitespace key",
            ),
        ):
            with self.subTest(content_type=response.headers.get("content-type")):
                client = HttpProviderSecretBroker(
                    base_url="https://secret-broker:8443",
                    ssl_context=_context(),
                    transport=httpx.MockTransport(lambda _, value=response: value),
                )
                with self.assertRaisesRegex(
                    InternalModelGatewayHttpError, "SECRET_BROKER_RESPONSE_INVALID"
                ):
                    await client.read_provider_key(_secret_request())

    def test_internal_urls_are_pinned_to_https_service_identities(self) -> None:
        for value in (
            "http://model-gateway-control:8443",
            "https://evil.invalid:8443",
            "https://model-gateway-control:9443",
            "https://model-gateway-control:8443/path",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                InternalModelGatewayHttpError, "INTERNAL_SERVICE_BASE_URL_INVALID"
            ):
                HttpModelEgressAdmission(base_url=value, ssl_context=_context())

    def test_tls_builder_requires_files_and_loads_a_client_chain(self) -> None:
        missing = Path("/definitely/missing")
        with self.assertRaisesRegex(
            InternalModelGatewayHttpError, "INTERNAL_MTLS_MATERIAL_UNAVAILABLE"
        ):
            build_workload_mtls_context(
                ca_file=missing,
                client_cert=missing,
                client_key=missing,
            )

        context = MagicMock(spec=ssl.SSLContext)
        with (
            patch.object(Path, "is_file", return_value=True),
            patch("ssl.create_default_context", return_value=context) as create,
        ):
            result = build_workload_mtls_context(
                ca_file=Path("ca.crt"),
                client_cert=Path("bridge.crt"),
                client_key=Path("bridge.key"),
            )
        self.assertIs(result, context)
        create.assert_called_once_with(cafile="ca.crt")
        context.load_cert_chain.assert_called_once_with("bridge.crt", "bridge.key")


if __name__ == "__main__":
    unittest.main()
