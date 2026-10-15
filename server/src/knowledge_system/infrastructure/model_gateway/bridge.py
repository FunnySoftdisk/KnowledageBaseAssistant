"""Bounded final-hop SSE bridge from internal Bifrost to Bailian."""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from uuid import UUID

import httpx

from knowledge_system.modules.model_gateway.application.egress import (
    EgressAdmissionOutcome,
    EgressAdmissionRequestV1,
    ModelEgressAdmission,
)
from knowledge_system.modules.model_gateway.application.secrets import (
    ProviderSecretBroker,
    ProviderSecretReadRequestV1,
)
from knowledge_system.modules.model_gateway.domain.chat_digest import (
    ProviderChatRequestV1,
    digest_provider_chat_request,
)


class ModelEgressBridgeError(RuntimeError):
    """Stable bridge failure code; never contains request, response or credential text."""

    def __init__(self, code: str, *, http_status: int | None = None) -> None:
        self.code = code
        self.http_status = http_status
        super().__init__(code)


class BailianEgressBridge:
    """Admit, fetch one exact secret version, inject it, and stream one fixed HTTPS path."""

    def __init__(
        self,
        *,
        provider_base_url: str,
        admission: ModelEgressAdmission,
        secret_broker: ProviderSecretBroker,
        transport: httpx.AsyncBaseTransport | None = None,
        verify: bool | str = True,
        timeout_seconds: float = 120.0,
    ) -> None:
        parsed = httpx.URL(provider_base_url)
        if (
            parsed.scheme != "https"
            or re.fullmatch(
                r"ws-[a-z0-9]+\.cn-beijing\.maas\.aliyuncs\.com",
                parsed.host or "",
            )
            is None
            or parsed.port not in (None, 443)
            or parsed.userinfo
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/compatible-mode/v1"
        ):
            raise ModelEgressBridgeError("BAILIAN_BASE_URL_INVALID")
        self._provider_base_url = str(parsed).rstrip("/")
        self._admission = admission
        self._secret_broker = secret_broker
        self._transport = transport
        self._verify = verify
        self._timeout = httpx.Timeout(timeout_seconds, connect=10.0)

    async def stream(
        self,
        *,
        model_call_id: UUID,
        route_ref: str,
        request: ProviderChatRequestV1,
    ) -> AsyncGenerator[bytes, None]:
        decision = await self._admission.admit(
            request=EgressAdmissionRequestV1(
                model_call_id=model_call_id,
                route_ref=route_ref,
                body=digest_provider_chat_request(request),
            )
        )
        if decision.outcome is not EgressAdmissionOutcome.ADMITTED or decision.permit is None:
            raise ModelEgressBridgeError(decision.reason_code or "MODEL_EGRESS_NOT_ADMITTED")
        permit = decision.permit
        if permit.provider_model != request.model:
            raise ModelEgressBridgeError("MODEL_EGRESS_PERMIT_MODEL_MISMATCH")

        provider_key = await self._secret_broker.read_provider_key(
            ProviderSecretReadRequestV1(
                model_call_id=model_call_id,
                route_ref=route_ref,
                secret_ref=permit.secret_ref,
                secret_version=permit.secret_version,
            )
        )
        try:
            try:
                authorization = "Bearer " + provider_key.decode("utf-8")
            except UnicodeDecodeError:
                raise ModelEgressBridgeError("PROVIDER_API_KEY_INVALID") from None
            try:
                async with httpx.AsyncClient(
                    verify=self._verify,
                    transport=self._transport,
                    timeout=self._timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    async with client.stream(
                        "POST",
                        f"{self._provider_base_url}/chat/completions",
                        headers={
                            "Authorization": authorization,
                            "Content-Type": "application/json",
                            "Accept": "text/event-stream",
                        },
                        content=request.model_dump_json(by_alias=True),
                    ) as response:
                        if response.status_code != 200:
                            raise ModelEgressBridgeError(
                                "BAILIAN_RESPONSE_REJECTED",
                                http_status=response.status_code,
                            )
                        async for chunk in response.aiter_bytes():
                            yield chunk
            except ModelEgressBridgeError:
                raise
            except httpx.TimeoutException:
                raise ModelEgressBridgeError("BAILIAN_TIMEOUT") from None
            except httpx.HTTPError:
                raise ModelEgressBridgeError("BAILIAN_CONNECTION_FAILED") from None
        finally:
            _zeroize(provider_key)


def _zeroize(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


__all__ = ["BailianEgressBridge", "ModelEgressBridgeError"]
