"""Private FastAPI process factories for Admission, Secret Broker and Egress Bridge."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from knowledge_system.modules.model_gateway.application.egress import (
    EgressAdmissionDecisionV1,
    EgressAdmissionRequestV1,
    ModelEgressAdmission,
)
from knowledge_system.modules.model_gateway.application.secrets import (
    ProviderSecretBroker,
    ProviderSecretReadRequestV1,
)
from knowledge_system.modules.model_gateway.domain.chat_digest import ProviderChatRequestV1

from .bridge import BailianEgressBridge, ModelEgressBridgeError
from .internal_http import InternalModelGatewayHttpError
from .openbao import OpenBaoSecretError

_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_ROUTE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")


class _RequestBodyError(RuntimeError):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


async def _strict_json_body[ContractT: BaseModel](
    request: Request,
    contract: type[ContractT],
    *,
    max_bytes: int,
) -> ContractT:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise _RequestBodyError("CONTENT_TYPE_INVALID", 415)
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            raise _RequestBodyError("CONTENT_LENGTH_INVALID") from None
        if declared < 1 or declared > max_bytes:
            raise _RequestBodyError("REQUEST_BODY_SIZE_INVALID", 413)
    body = await request.body()
    if not body or len(body) > max_bytes:
        raise _RequestBodyError("REQUEST_BODY_SIZE_INVALID", 413)
    try:
        return contract.model_validate_json(body, strict=True)
    except (ValidationError, ValueError):
        raise _RequestBodyError("REQUEST_BODY_INVALID") from None


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"code": code, "retryable": status >= 500},
        headers={"Cache-Control": "no-store"},
    )


def create_admission_app(authority: ModelEgressAdmission) -> FastAPI:
    app = FastAPI(title="Knowledge Model Egress Admission", docs_url=None, redoc_url=None)

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.post(
        "/internal/v1/model-egress/admit",
        response_model=EgressAdmissionDecisionV1,
    )
    async def admit(request: Request) -> Response:
        try:
            body = await _strict_json_body(request, EgressAdmissionRequestV1, max_bytes=16_384)
        except _RequestBodyError as error:
            return _error(error.status, error.code)
        decision = await authority.admit(request=body)
        return JSONResponse(
            content=decision.model_dump(mode="json"),
            headers={"Cache-Control": "no-store"},
        )

    return app


def create_secret_broker_app(broker: ProviderSecretBroker) -> FastAPI:
    app = FastAPI(title="Knowledge Provider Secret Broker", docs_url=None, redoc_url=None)

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.post("/internal/v1/provider-secrets/read", response_class=Response)
    async def read_provider_secret(request: Request) -> Response:
        try:
            body = await _strict_json_body(
                request,
                ProviderSecretReadRequestV1,
                max_bytes=8_192,
            )
        except _RequestBodyError as error:
            return _error(error.status, error.code)
        try:
            value = await broker.read_provider_key(body)
        except OpenBaoSecretError as error:
            status = 403 if error.code == "PROVIDER_SECRET_READ_NOT_AUTHORIZED" else 503
            return _error(status, error.code)
        try:
            response_body = bytes(value)
        finally:
            _zeroize(value)
        return Response(
            content=response_body,
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    return app


def create_bridge_app(bridge: BailianEgressBridge) -> FastAPI:
    app = FastAPI(title="Knowledge Model Egress Bridge", docs_url=None, redoc_url=None)

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        try:
            body_request = await _strict_json_body(
                request,
                ProviderChatRequestV1,
                max_bytes=2_097_152,
            )
        except _RequestBodyError as error:
            return _error(error.status, error.code)

        model_call_id = request.headers.get("x-knowledge-model-call-id", "")
        route_ref = request.headers.get("x-knowledge-route-ref", "")
        trace_id = request.headers.get("x-knowledge-trace-id", "")
        try:
            call_id = UUID(model_call_id)
        except ValueError:
            return _error(400, "MODEL_CALL_ID_INVALID")
        if _ROUTE_REF.fullmatch(route_ref) is None:
            return _error(400, "ROUTE_REF_INVALID")
        if _TRACE_ID.fullmatch(trace_id) is None or trace_id == "0" * 32:
            return _error(400, "TRACE_ID_INVALID")

        stream = bridge.stream(model_call_id=call_id, route_ref=route_ref, request=body_request)
        try:
            first = await anext(stream)
        except StopAsyncIteration:
            first = None
        except InternalModelGatewayHttpError as error:
            await stream.aclose()
            return _error(503, error.code)
        except ModelEgressBridgeError as error:
            await stream.aclose()
            if error.code.startswith("MODEL_EGRESS") or error.code.startswith("EGRESS_"):
                return _error(403, error.code)
            status = error.http_status if error.http_status in {400, 401, 403, 429, 503} else 502
            return _error(status, error.code)

        async def response_stream() -> AsyncIterator[bytes]:
            try:
                if first is not None:
                    yield first
                async for chunk in stream:
                    yield chunk
            finally:
                await stream.aclose()

        return StreamingResponse(
            response_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app


def _zeroize(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


__all__ = ["create_admission_app", "create_bridge_app", "create_secret_broker_app"]
