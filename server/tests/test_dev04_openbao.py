"""DEV-04 OpenBao SecretRef与KV v2适配器离线测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import httpx

from knowledge_system.infrastructure.model_gateway.openbao import (
    AuthorizedOpenBaoSecretBroker,
    OpenBaoAgentFileTokenProvider,
    OpenBaoKv2Client,
    OpenBaoSecretError,
)
from knowledge_system.modules.model_gateway.application.secrets import ProviderSecretReadRequestV1
from knowledge_system.modules.model_gateway.domain.secret_reference import (
    SecretReferenceViolation,
    derive_provider_secret_path,
)


class _TokenProvider:
    def __init__(self, value: bytes = b"operator-token-value") -> None:
        self.value = value
        self.issued: list[bytearray] = []

    async def token(self) -> bytearray:
        token = bytearray(self.value)
        self.issued.append(token)
        return token


class SecretReferenceTests(unittest.TestCase):
    def test_path_is_deterministic_and_does_not_disclose_materials(self) -> None:
        kwargs = {
            "identity_hmac_key": b"h" * 32,
            "workspace_id": "ws-example123",
            "provider": "alibaba_bailian",
            "region": "cn-beijing",
        }
        first = derive_provider_secret_path(**kwargs)
        second = derive_provider_secret_path(**kwargs)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^model-provider/[0-9a-f]{64}$")
        self.assertNotIn("example", first)
        self.assertNotIn("alibaba", first)
        self.assertNotIn("beijing", first)

    def test_rejects_short_hmac_key_and_invalid_identifiers(self) -> None:
        with self.assertRaisesRegex(SecretReferenceViolation, "SECRET_REF_HMAC_KEY_TOO_SHORT"):
            derive_provider_secret_path(
                identity_hmac_key=b"short",
                workspace_id="ws-example123",
                provider="alibaba_bailian",
                region="cn-beijing",
            )
        with self.assertRaisesRegex(SecretReferenceViolation, "SECRET_REF_WORKSPACE_INVALID"):
            derive_provider_secret_path(
                identity_hmac_key=b"h" * 32,
                workspace_id="email@example.invalid",
                provider="alibaba_bailian",
                region="cn-beijing",
            )


class OpenBaoKv2ClientTests(unittest.IsolatedAsyncioTestCase):
    def _client(
        self,
        handler: httpx.MockTransport,
        token_provider: _TokenProvider,
    ) -> OpenBaoKv2Client:
        return OpenBaoKv2Client(
            base_url="https://bao.internal:8200",
            mount="knowledge-model",
            token_provider=token_provider,
            transport=handler,
        )

    async def test_write_uses_cas_and_returns_secret_ref_without_secret(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"data": {"version": 1}})

        token_provider = _TokenProvider()
        client = self._client(httpx.MockTransport(handle), token_provider)
        api_key = bytearray(b"synthetic-provider-key-value")
        receipt = await client.write_provider_key(
            secret_path="model-provider/" + "a" * 64,
            api_key=api_key,
            expected_version=0,
        )

        self.assertEqual(receipt.version, 1)
        self.assertEqual(
            receipt.secret_ref,
            "openbao-kv-v2://knowledge-model/model-provider/" + "a" * 64,
        )
        self.assertEqual(
            requests[0].url.path, "/v1/knowledge-model/data/model-provider/" + "a" * 64
        )
        self.assertEqual(json.loads(requests[0].content)["options"], {"cas": 0})
        self.assertNotIn("synthetic-provider-key-value", receipt.model_dump_json())
        self.assertEqual(token_provider.issued[0], bytearray(len(b"operator-token-value")))
        self.assertEqual(api_key, bytearray(b"synthetic-provider-key-value"))

    async def test_read_requires_exact_version_and_zeroizes_token(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params["version"], "2")
            return httpx.Response(
                200,
                json={
                    "data": {
                        "data": {"api_key": "synthetic-provider-key-value"},
                        "metadata": {"version": 2},
                    }
                },
            )

        token_provider = _TokenProvider()
        client = self._client(httpx.MockTransport(handle), token_provider)
        value = await client.read_provider_key(
            secret_path="model-provider/" + "b" * 64,
            version=2,
        )
        self.assertEqual(value, bytearray(b"synthetic-provider-key-value"))
        self.assertEqual(token_provider.issued[0], bytearray(len(b"operator-token-value")))

    async def test_errors_do_not_include_upstream_body_or_secret(self) -> None:
        def handle(_: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="sensitive-upstream-error")

        client = self._client(httpx.MockTransport(handle), _TokenProvider())
        with self.assertRaisesRegex(OpenBaoSecretError, "OPENBAO_WRITE_FORBIDDEN") as raised:
            await client.write_provider_key(
                secret_path="model-provider/" + "c" * 64,
                api_key=bytearray(b"synthetic-provider-key-value"),
                expected_version=0,
            )
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertNotIn("synthetic", str(raised.exception))

    async def test_rejects_http_arbitrary_path_and_bad_key(self) -> None:
        with self.assertRaisesRegex(OpenBaoSecretError, "OPENBAO_BASE_URL_INVALID"):
            OpenBaoKv2Client(
                base_url="http://bao.internal:8200",
                mount="knowledge-model",
                token_provider=_TokenProvider(),
            )
        client = self._client(httpx.MockTransport(lambda _: httpx.Response(500)), _TokenProvider())
        with self.assertRaisesRegex(OpenBaoSecretError, "OPENBAO_SECRET_PATH_INVALID"):
            await client.read_provider_key(secret_path="../../root", version=1)
        with self.assertRaisesRegex(OpenBaoSecretError, "PROVIDER_API_KEY_INVALID"):
            await client.write_provider_key(
                secret_path="model-provider/" + "d" * 64,
                api_key=bytearray(b"too-short"),
                expected_version=0,
            )


class _Authorizer:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.calls: list[ProviderSecretReadRequestV1] = []

    async def authorize(self, request: ProviderSecretReadRequestV1) -> bool:
        self.calls.append(request)
        return self.allowed


class OpenBaoBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_token_file_requires_exact_0600_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bao-token"
            path.write_bytes(b"short-lived-token\n")
            path.chmod(0o600)
            provider = OpenBaoAgentFileTokenProvider(path)
            self.assertEqual(await provider.token(), bytearray(b"short-lived-token"))

            path.chmod(0o640)
            with self.assertRaisesRegex(
                OpenBaoSecretError, "OPENBAO_AGENT_TOKEN_PERMISSIONS_INVALID"
            ):
                await provider.token()

    async def test_broker_authorizes_before_exact_version_read(self) -> None:
        calls: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "data": {"api_key": "synthetic-provider-key-value"},
                        "metadata": {"version": 1},
                    }
                },
            )

        authorizer = _Authorizer(True)
        reader = OpenBaoKv2Client(
            base_url="https://bao.internal:8200",
            mount="knowledge-model",
            token_provider=_TokenProvider(),
            transport=httpx.MockTransport(handle),
        )
        broker = AuthorizedOpenBaoSecretBroker(reader=reader, authorizer=authorizer)
        request = ProviderSecretReadRequestV1(
            model_call_id=uuid4(),
            route_ref="route/planning-strong/v1",
            secret_ref=("openbao-kv-v2://knowledge-model/model-provider/" + "e" * 64),
            secret_version=1,
        )
        key = await broker.read_provider_key(request)

        self.assertEqual(key, bytearray(b"synthetic-provider-key-value"))
        self.assertEqual(authorizer.calls, [request])
        self.assertEqual(calls[0].url.params["version"], "1")

    async def test_broker_denial_never_reads_openbao(self) -> None:
        calls: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(500)

        broker = AuthorizedOpenBaoSecretBroker(
            reader=OpenBaoKv2Client(
                base_url="https://bao.internal:8200",
                mount="knowledge-model",
                token_provider=_TokenProvider(),
                transport=httpx.MockTransport(handle),
            ),
            authorizer=_Authorizer(False),
        )
        request = ProviderSecretReadRequestV1(
            model_call_id=uuid4(),
            route_ref="route/planning-strong/v1",
            secret_ref=("openbao-kv-v2://knowledge-model/model-provider/" + "f" * 64),
            secret_version=1,
        )
        with self.assertRaisesRegex(OpenBaoSecretError, "PROVIDER_SECRET_READ_NOT_AUTHORIZED"):
            await broker.read_provider_key(request)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
