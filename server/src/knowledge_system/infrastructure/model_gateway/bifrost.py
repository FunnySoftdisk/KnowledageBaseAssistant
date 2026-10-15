"""HTTP/SSE client for the pinned internal Bifrost data plane."""

from __future__ import annotations

import json
import ssl
from collections.abc import AsyncIterator

import httpx

from knowledge_system.modules.model_gateway.domain.chat_request import BifrostChatRequestV1
from knowledge_system.modules.model_gateway.domain.provider_error import ProviderErrorSource
from knowledge_system.modules.model_gateway.domain.stream_event import (
    BifrostDeltaEventV1,
    BifrostErrorEventV1,
    BifrostEventV1,
    BifrostUsageEventV1,
)


class BifrostClientError(RuntimeError):
    """Stable infrastructure failure code; never contains response or prompt content."""


class BifrostHttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        verify: bool | str = True,
        cert: str | tuple[str, str] | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        parsed = httpx.URL(base_url)
        if parsed.scheme != "https" or parsed.query or parsed.fragment or parsed.path != "/":
            raise BifrostClientError("BIFROST_BASE_URL_INVALID")
        self._base_url = str(parsed).rstrip("/")
        self._transport = transport
        if cert is not None:
            if not isinstance(verify, str):
                raise BifrostClientError("BIFROST_MTLS_CA_FILE_REQUIRED")
            try:
                context = ssl.create_default_context(cafile=verify)
                context.minimum_version = ssl.TLSVersion.TLSv1_3
                context.load_cert_chain(*cert)
            except (OSError, ssl.SSLError, ValueError):
                raise BifrostClientError("BIFROST_MTLS_MATERIAL_INVALID") from None
            self._verify: bool | str | ssl.SSLContext = context
        else:
            self._verify = verify
        self._timeout = httpx.Timeout(timeout_seconds, connect=10.0)

    async def stream(
        self,
        *,
        model_call_id: str,
        trace_id: str,
        route_ref: str,
        request: BifrostChatRequestV1,
    ) -> AsyncIterator[BifrostEventV1]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "x-bf-eh-x-knowledge-model-call-id": model_call_id,
            "x-bf-eh-x-knowledge-route-ref": route_ref,
            "x-bf-eh-x-knowledge-trace-id": trace_id,
        }
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
                    f"{self._base_url}/v1/chat/completions",
                    headers=headers,
                    content=request.model_dump_json(by_alias=True),
                ) as response:
                    if response.status_code != 200:
                        yield BifrostErrorEventV1(type="ERROR", http_status=response.status_code)
                        return
                    frame_index = 0
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            return
                        frame_index += 1
                        event = _parse_sse_payload(payload, frame_index)
                        if event is None:
                            continue
                        yield event
        except httpx.TimeoutException:
            yield BifrostErrorEventV1(type="ERROR", source=ProviderErrorSource.TIMEOUT)
        except httpx.HTTPError:
            yield BifrostErrorEventV1(type="ERROR", source=ProviderErrorSource.CONNECTION)


def _parse_sse_payload(payload: str, frame_index: int) -> BifrostEventV1 | None:
    try:
        document = json.loads(payload)
    except (TypeError, ValueError):
        return BifrostErrorEventV1(type="ERROR", source=ProviderErrorSource.STREAM)
    if not isinstance(document, dict):
        return BifrostErrorEventV1(type="ERROR", source=ProviderErrorSource.STREAM)

    usage = document.get("usage")
    if isinstance(usage, dict):
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        if type(input_tokens) is int and type(output_tokens) is int:
            return BifrostUsageEventV1(
                type="USAGE",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, dict):
        return BifrostErrorEventV1(type="ERROR", source=ProviderErrorSource.STREAM)
    delta = choice.get("delta")
    content = delta.get("content") if isinstance(delta, dict) else None
    if content is not None and not isinstance(content, str):
        return BifrostErrorEventV1(type="ERROR", source=ProviderErrorSource.STREAM)
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        return BifrostErrorEventV1(type="ERROR", source=ProviderErrorSource.STREAM)
    response_id = document.get("id")
    chunk_id = f"{response_id or 'bifrost'}:{frame_index}"
    return BifrostDeltaEventV1(
        type="DELTA",
        chunk_id=chunk_id,
        text_delta=content,
        finish_reason=finish_reason,
    )


__all__ = ["BifrostClientError", "BifrostHttpClient"]
