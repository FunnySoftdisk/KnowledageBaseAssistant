"""mTLS HTTP clients for Bridge to call Admission and Secret Broker services."""

from __future__ import annotations

import re
import ssl
from pathlib import Path

import httpx

from knowledge_system.modules.model_gateway.application.egress import (
    EgressAdmissionDecisionV1,
    EgressAdmissionOutcome,
    EgressAdmissionRequestV1,
)
from knowledge_system.modules.model_gateway.application.secrets import ProviderSecretReadRequestV1


class InternalModelGatewayHttpError(RuntimeError):
    """Stable internal transport code; response bodies and credentials are never attached."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def build_workload_mtls_context(
    *, ca_file: Path, client_cert: Path, client_key: Path
) -> ssl.SSLContext:
    if not ca_file.is_file() or not client_cert.is_file() or not client_key.is_file():
        raise InternalModelGatewayHttpError("INTERNAL_MTLS_MATERIAL_UNAVAILABLE")
    try:
        context = ssl.create_default_context(cafile=str(ca_file))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(str(client_cert), str(client_key))
    except (OSError, ssl.SSLError, ValueError):
        raise InternalModelGatewayHttpError("INTERNAL_MTLS_MATERIAL_INVALID") from None
    return context


class _InternalHttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        expected_host: str,
        ssl_context: ssl.SSLContext,
        transport: httpx.AsyncBaseTransport | None,
        timeout_seconds: float,
    ) -> None:
        parsed = httpx.URL(base_url)
        if (
            parsed.scheme != "https"
            or parsed.host != expected_host
            or parsed.port not in (None, 443, 8443)
            or parsed.userinfo
            or parsed.query
            or parsed.fragment
            or parsed.path != "/"
        ):
            raise InternalModelGatewayHttpError("INTERNAL_SERVICE_BASE_URL_INVALID")
        if not isinstance(ssl_context, ssl.SSLContext):
            raise InternalModelGatewayHttpError("INTERNAL_MTLS_CONTEXT_REQUIRED")
        self._base_url = str(parsed).rstrip("/")
        self._ssl_context = ssl_context
        self._transport = transport
        self._timeout = httpx.Timeout(timeout_seconds, connect=5.0)

    async def post(self, path: str, *, content: str) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                verify=self._ssl_context,
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                return await client.post(
                    f"{self._base_url}{path}",
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    content=content,
                )
        except httpx.TimeoutException:
            raise InternalModelGatewayHttpError("INTERNAL_SERVICE_TIMEOUT") from None
        except httpx.HTTPError:
            raise InternalModelGatewayHttpError("INTERNAL_SERVICE_UNAVAILABLE") from None


class HttpModelEgressAdmission:
    """Bridge-side client for the PG-backed control-plane admission endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        ssl_context: ssl.SSLContext,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = _InternalHttpClient(
            base_url=base_url,
            expected_host="model-gateway-control",
            ssl_context=ssl_context,
            transport=transport,
            timeout_seconds=timeout_seconds,
        )

    async def admit(self, *, request: EgressAdmissionRequestV1) -> EgressAdmissionDecisionV1:
        try:
            response = await self._client.post(
                "/internal/v1/model-egress/admit",
                content=request.model_dump_json(),
            )
        except InternalModelGatewayHttpError as error:
            return EgressAdmissionDecisionV1(
                outcome=EgressAdmissionOutcome.RECONCILING,
                reason_code=error.code,
            )
        if response.status_code != 200:
            return EgressAdmissionDecisionV1(
                outcome=EgressAdmissionOutcome.RECONCILING,
                reason_code="EGRESS_ADMISSION_SERVICE_REJECTED",
            )
        try:
            return EgressAdmissionDecisionV1.model_validate_json(response.content, strict=True)
        except (ValueError, TypeError):
            return EgressAdmissionDecisionV1(
                outcome=EgressAdmissionOutcome.RECONCILING,
                reason_code="EGRESS_ADMISSION_RESPONSE_INVALID",
            )


class HttpProviderSecretBroker:
    """Bridge-side client; receives one provider key over the isolated mTLS link."""

    def __init__(
        self,
        *,
        base_url: str,
        ssl_context: ssl.SSLContext,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = _InternalHttpClient(
            base_url=base_url,
            expected_host="secret-broker",
            ssl_context=ssl_context,
            transport=transport,
            timeout_seconds=timeout_seconds,
        )

    async def read_provider_key(self, request: ProviderSecretReadRequestV1) -> bytearray:
        response = await self._client.post(
            "/internal/v1/provider-secrets/read",
            content=request.model_dump_json(),
        )
        if response.status_code != 200:
            raise InternalModelGatewayHttpError("SECRET_BROKER_READ_REJECTED")
        if response.headers.get("content-type", "").split(";", 1)[0] != "application/octet-stream":
            raise InternalModelGatewayHttpError("SECRET_BROKER_RESPONSE_INVALID")
        value = bytearray(response.content)
        if not 16 <= len(value) <= 512 or re.search(rb"[\x00-\x20\x7f]", value):
            _zeroize(value)
            raise InternalModelGatewayHttpError("SECRET_BROKER_RESPONSE_INVALID")
        return value


def _zeroize(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


__all__ = [
    "HttpModelEgressAdmission",
    "HttpProviderSecretBroker",
    "InternalModelGatewayHttpError",
    "build_workload_mtls_context",
]
