"""DEV-05 批2：OpenBao KV v2元数据读取（`read_current_version`）离线验收。

以`httpx.MockTransport`验证：元数据端点路径、`current_version`解析、稳定错误码映射（404/403/
载荷非法）与Token即时清零。不接真实OpenBao、不读Provider Key正文。
"""

from __future__ import annotations

import unittest

import httpx

from knowledge_system.infrastructure.model_gateway.openbao import (
    OpenBaoKv2Client,
    OpenBaoSecretError,
)


class _TokenProvider:
    def __init__(self) -> None:
        self.issued: list[bytearray] = []

    async def token(self) -> bytearray:
        token = bytearray(b"operator-token-value")
        self.issued.append(token)
        return token


class OpenBaoMetadataTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, handler: httpx.MockTransport) -> OpenBaoKv2Client:
        return OpenBaoKv2Client(
            base_url="https://bao.internal:8200",
            mount="knowledge-model",
            token_provider=_TokenProvider(),
            transport=handler,
        )

    async def test_reads_current_version_from_metadata_endpoint(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"data": {"current_version": 3}})

        token_provider = _TokenProvider()
        client = OpenBaoKv2Client(
            base_url="https://bao.internal:8200",
            mount="knowledge-model",
            token_provider=token_provider,
            transport=httpx.MockTransport(handle),
        )
        version = await client.read_current_version(secret_path="model-provider/" + "a" * 64)

        self.assertEqual(version, 3)
        self.assertEqual(
            requests[0].url.path,
            "/v1/knowledge-model/metadata/model-provider/" + "a" * 64,
        )
        self.assertEqual(token_provider.issued[0], bytearray(len(b"operator-token-value")))

    async def test_not_found_maps_to_stable_code(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(404)

        client = self._client(httpx.MockTransport(handle))
        with self.assertRaisesRegex(OpenBaoSecretError, "OPENBAO_SECRET_NOT_FOUND"):
            await client.read_current_version(secret_path="model-provider/" + "a" * 64)

    async def test_forbidden_maps_to_metadata_code(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(403)

        client = self._client(httpx.MockTransport(handle))
        with self.assertRaisesRegex(OpenBaoSecretError, "OPENBAO_METADATA_FORBIDDEN"):
            await client.read_current_version(secret_path="model-provider/" + "a" * 64)

    async def test_invalid_payload_maps_to_stable_code(self) -> None:
        for payload in (
            {"data": {}},
            {"data": {"current_version": "3"}},
            {"data": {"current_version": 0}},
        ):

            def handle(request: httpx.Request, payload: object = payload) -> httpx.Response:
                del request
                return httpx.Response(200, json=payload)

            client = self._client(httpx.MockTransport(handle))
            with self.assertRaisesRegex(OpenBaoSecretError, "OPENBAO_METADATA_RESPONSE_INVALID"):
                await client.read_current_version(secret_path="model-provider/" + "a" * 64)


if __name__ == "__main__":
    unittest.main()
