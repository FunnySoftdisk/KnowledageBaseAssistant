"""统一错误信封与异常处理器；code为稳定机器契约，HTTP状态按code映射。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final, cast
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from knowledge_system.infrastructure.persistence import (
    IdempotencyKeyReusedError,
    PersistenceContractError,
    TaskCreationWriteConflictError,
)
from knowledge_system.modules.iam.public import AuthError, AuthErrorCode
from knowledge_system.modules.tasking.public import TaskEntryError

_HTTP_AUTH: Final[dict[AuthErrorCode, int]] = {
    AuthErrorCode.AUTH_INVALID_CREDENTIALS: 401,
    AuthErrorCode.AUTH_TOKEN_EXPIRED: 401,
    AuthErrorCode.AUTH_TOKEN_INVALID: 401,
    AuthErrorCode.AUTH_SESSION_REVOKED: 401,
    AuthErrorCode.AUTH_PASSWORD_CHANGE_REQUIRED: 403,
    AuthErrorCode.AUTH_ACCOUNT_DISABLED: 403,
    AuthErrorCode.AUTH_ACCOUNT_TEMP_LOCKED: 423,
}

_HTTP_TASK: Final[dict[str, int]] = {
    "CONVERSATION_NOT_FOUND": 404,
    "TASK_ATTACHMENT_INVALID": 422,
    "TASK_NOT_FOUND": 404,
    "OUTPUT_CONTRACT_REF_NOT_FOUND": 422,
    "OUTPUT_CONTRACT_REF_STALE": 422,
    "OUTPUT_CONTRACT_INVALID": 422,
    "OUTPUT_CONTRACT_PROFILE_INVALID": 500,
}

_HTTP_PARSE: Final[dict[str, int]] = {
    "IDEMPOTENCY_KEY_INVALID": 422,
    "CREATE_TASK_BODY_SIZE_LIMIT": 413,
    "CREATE_TASK_BODY_UTF8_INVALID": 422,
    "CREATE_TASK_JSON_INVALID": 422,
    "DUPLICATE_JSON_KEY": 422,
    "REQUEST_VALIDATION_FAILED": 422,
}

_MESSAGES: Final[dict[str, str]] = {
    "AUTH_INVALID_CREDENTIALS": "Invalid username or password.",
    "AUTH_TOKEN_EXPIRED": "Access token has expired.",
    "AUTH_TOKEN_INVALID": "Access token is missing or invalid.",
    "AUTH_SESSION_REVOKED": "Session has been revoked.",
    "AUTH_PASSWORD_CHANGE_REQUIRED": "Password change is required.",
    "AUTH_ACCOUNT_DISABLED": "Account is disabled.",
    "AUTH_ACCOUNT_TEMP_LOCKED": "Account is temporarily locked.",
    "CONVERSATION_NOT_FOUND": "Conversation not found.",
    "TASK_ATTACHMENT_INVALID": "One or more attachments are not available.",
    "TASK_NOT_FOUND": "Task not found.",
    "OUTPUT_CONTRACT_REF_NOT_FOUND": "Output contract reference not found.",
    "OUTPUT_CONTRACT_REF_STALE": "Output contract reference is stale.",
    "OUTPUT_CONTRACT_INVALID": "Output contract is invalid.",
    "OUTPUT_CONTRACT_PROFILE_INVALID": "Output contract profile is misconfigured.",
    "IDEMPOTENCY_KEY_REUSED": "Idempotency key was reused with a different request.",
    "IDEMPOTENCY_KEY_INVALID": "Idempotency key must be a UUID.",
    "CREATE_TASK_BODY_SIZE_LIMIT": "Request body exceeds the size limit.",
    "CREATE_TASK_BODY_UTF8_INVALID": "Request body is not valid UTF-8.",
    "CREATE_TASK_JSON_INVALID": "Request body is not valid JSON.",
    "DUPLICATE_JSON_KEY": "Request body contains duplicate JSON keys.",
    "REQUEST_VALIDATION_FAILED": "Request body failed validation.",
    "TASK_CREATION_WRITE_CONFLICT": "Task creation raced with a concurrent request.",
    "INTERNAL_ERROR": "An internal error occurred.",
}


class FieldError(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    field: str
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    code: str
    message: str
    retryable: bool
    retry_after_seconds: int | None = None
    details: dict[str, object] | None = None
    field_errors: tuple[FieldError, ...] = ()
    request_id: str
    trace_id: str
    server_time: datetime


class RequestParseError(RuntimeError):
    """请求体或头部解析失败；code∈_HTTP_PARSE。"""

    def __init__(self, code: str, *, field_errors: tuple[FieldError, ...] = ()) -> None:
        self.code = code
        self.field_errors = field_errors
        super().__init__(code)


def message_for(code: str) -> str:
    return _MESSAGES.get(code, code.replace("_", " ").capitalize())


def pydantic_field_errors(errors: Sequence[object]) -> tuple[FieldError, ...]:
    result: list[FieldError] = []
    for raw in errors:
        item = cast(dict[str, object], raw)
        loc = item.get("loc", ())
        parts = loc if isinstance(loc, (list, tuple)) else (loc,)
        field = ".".join(str(part) for part in parts) if parts else "<root>"
        result.append(
            FieldError(
                field=field,
                code=str(item.get("type", "value_error")),
                message=str(item.get("msg", "")),
            )
        )
    return tuple(result)


def _request_ids(request: Request) -> tuple[str, str]:
    request_id = getattr(request.state, "request_id", None) or uuid4().hex
    trace_id = getattr(request.state, "trace_id", None) or uuid4().hex
    return request_id, trace_id


def envelope(
    request: Request,
    *,
    status: int,
    code: str,
    retryable: bool,
    retry_after_seconds: int | None = None,
    details: dict[str, object] | None = None,
    field_errors: tuple[FieldError, ...] = (),
) -> ErrorEnvelope:
    request_id, trace_id = _request_ids(request)
    return ErrorEnvelope(
        code=code,
        message=message_for(code),
        retryable=retryable,
        retry_after_seconds=retry_after_seconds,
        details=details,
        field_errors=field_errors,
        request_id=request_id,
        trace_id=trace_id,
        server_time=datetime.now(UTC),
    )


def response(
    request: Request,
    *,
    status: int,
    code: str,
    retryable: bool,
    retry_after_seconds: int | None = None,
    details: dict[str, object] | None = None,
    field_errors: tuple[FieldError, ...] = (),
) -> JSONResponse:
    body = envelope(
        request,
        status=status,
        code=code,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds,
        details=details,
        field_errors=field_errors,
    )
    headers: dict[str, str] = {"X-Request-Id": body.request_id, "X-Trace-Id": body.trace_id}
    if retry_after_seconds is not None:
        headers["Retry-After"] = str(retry_after_seconds)
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"), headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AuthError)
    async def _auth(request: Request, error: AuthError) -> JSONResponse:
        status = _HTTP_AUTH.get(error.code, 401)
        return response(request, status=status, code=error.code.value, retryable=error.retryable)

    @app.exception_handler(TaskEntryError)
    async def _task(request: Request, error: TaskEntryError) -> JSONResponse:
        status = _HTTP_TASK.get(error.code, 500)
        return response(request, status=status, code=error.code, retryable=False)

    @app.exception_handler(RequestParseError)
    async def _parse(request: Request, error: RequestParseError) -> JSONResponse:
        status = _HTTP_PARSE.get(error.code, 422)
        return response(
            request,
            status=status,
            code=error.code,
            retryable=False,
            field_errors=error.field_errors,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        field_errors = pydantic_field_errors(error.errors())
        return response(
            request,
            status=422,
            code="REQUEST_VALIDATION_FAILED",
            retryable=False,
            field_errors=field_errors,
        )

    @app.exception_handler(IdempotencyKeyReusedError)
    async def _idempotency(request: Request, error: IdempotencyKeyReusedError) -> JSONResponse:
        del error
        return response(
            request, status=409, code="IDEMPOTENCY_KEY_REUSED", retryable=False
        )

    @app.exception_handler(TaskCreationWriteConflictError)
    async def _write_conflict(
        request: Request, error: TaskCreationWriteConflictError
    ) -> JSONResponse:
        del error
        return response(
            request,
            status=409,
            code="TASK_CREATION_WRITE_CONFLICT",
            retryable=True,
        )

    @app.exception_handler(PersistenceContractError)
    async def _contract(request: Request, error: PersistenceContractError) -> JSONResponse:
        del error
        return response(request, status=500, code="INTERNAL_ERROR", retryable=False)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, error: Exception) -> JSONResponse:
        del error
        return response(request, status=500, code="INTERNAL_ERROR", retryable=False)
