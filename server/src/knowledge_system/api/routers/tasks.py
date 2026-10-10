"""任务入口路由：创建（幂等）、详情、当前结果查询与事件流（SSE 续读）。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from knowledge_system.modules.iam.public import Subject
from knowledge_system.modules.tasking.public import (
    CreateTaskResult,
    TaskCancellationService,
    TaskCancelResult,
    TaskCreationService,
    TaskDetailView,
    TaskEventProjectionV1,
    TaskEventReconnectResultV1,
    TaskEventStreamService,
    TaskQueryService,
    TaskResultView,
    parse_create_task_request,
)

from ..dependencies import (
    get_subject,
    get_task_cancellation_service,
    get_task_creation_service,
    get_task_event_stream_service,
    get_task_query_service,
)
from ..errors import RequestParseError, pydantic_field_errors
from ..schemas import TaskCancelResponse, TaskCreateResponse

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


def _require_idempotency_key(request: Request) -> str:
    value = request.headers.get("idempotency-key")
    if value is None:
        raise RequestParseError("IDEMPOTENCY_KEY_INVALID")
    value = value.strip()
    try:
        UUID(value)
    except ValueError as error:
        raise RequestParseError("IDEMPOTENCY_KEY_INVALID") from error
    return value


def _to_create_response(result: CreateTaskResult) -> TaskCreateResponse:
    body = result.response_body
    return TaskCreateResponse(
        task_id=result.task_id,
        input_id=UUID(str(body["input_id"])),
        status=str(body["status"]),
        deadline_at=datetime.fromisoformat(str(body["deadline_at"])),
        active_plan_version=cast(int | None, body["active_plan_version"]),
    )


@router.post("", response_model=TaskCreateResponse, status_code=202)
async def create_task(
    request: Request,
    subject: Annotated[Subject, Depends(get_subject)],
    service: Annotated[TaskCreationService, Depends(get_task_creation_service)],
) -> TaskCreateResponse:
    idempotency_key = _require_idempotency_key(request)
    body = await request.body()
    try:
        parsed = parse_create_task_request(body)
    except ValidationError as error:
        raise RequestParseError(
            "REQUEST_VALIDATION_FAILED",
            field_errors=pydantic_field_errors(error.errors()),
        ) from error
    except ValueError as error:
        raise RequestParseError(str(error)) from error
    result = await service.create(
        parsed,
        subject,
        idempotency_key=idempotency_key,
        trace_id=request.state.trace_id,
    )
    return _to_create_response(result)


@router.get("/{task_id}/result", response_model=TaskResultView)
async def get_task_result(
    task_id: UUID,
    subject: Annotated[Subject, Depends(get_subject)],
    service: Annotated[TaskQueryService, Depends(get_task_query_service)],
) -> TaskResultView:
    return await service.get_result(task_id, subject)


@router.post("/{task_id}/cancel", response_model=TaskCancelResponse)
async def cancel_task(
    task_id: UUID,
    request: Request,
    subject: Annotated[Subject, Depends(get_subject)],
    service: Annotated[TaskCancellationService, Depends(get_task_cancellation_service)],
) -> TaskCancelResponse:
    result: TaskCancelResult = await service.cancel(
        task_id, subject, trace_id=request.state.trace_id
    )
    return TaskCancelResponse(task_id=result.task_id, status=result.status)


@router.get("/{task_id}", response_model=TaskDetailView)
async def get_task(
    task_id: UUID,
    subject: Annotated[Subject, Depends(get_subject)],
    service: Annotated[TaskQueryService, Depends(get_task_query_service)],
) -> TaskDetailView:
    return await service.get_detail(task_id, subject)


# SSE 重连提示间隔（毫秒）：本次连接只做有界续读，结束后由 EventSource 按 retry 重新连接，
# 并以 Last-Event-ID 继续。这样刷新/断线不丢状态，且不长期占用服务端连接。
SSE_RETRY_MILLISECONDS = 1000


def _json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _projection_frame(event: TaskEventProjectionV1) -> str:
    data = event.payload.model_dump(mode="json", exclude_none=True)
    return f"id: {event.sequence}\nevent: {event.event_type}\ndata: {_json(data)}\n\n"


def _terminal_frame(result: TaskEventReconnectResultV1) -> str:
    assert result.decision.terminal_display is not None
    data = {
        "status": result.status.value,
        "display": result.decision.terminal_display.value,
    }
    return f"event: terminal\ndata: {_json(data)}\n\n"


async def _sse_frames(result: TaskEventReconnectResultV1) -> AsyncIterator[str]:
    if result.decision.terminal_display is not None:
        yield _terminal_frame(result)
        return
    for event in result.events:
        yield _projection_frame(event)
    if result.decision.heartbeat_required:
        yield ": ping\n\n"
    yield f"retry: {SSE_RETRY_MILLISECONDS}\n\n"


@router.get("/{task_id}/events")
async def stream_task_events(
    task_id: UUID,
    request: Request,
    subject: Annotated[Subject, Depends(get_subject)],
    service: Annotated[TaskEventStreamService, Depends(get_task_event_stream_service)],
) -> StreamingResponse:
    last_event_id = request.headers.get("last-event-id")
    result = await service.reconnect(task_id, subject, last_event_id)
    return StreamingResponse(
        _sse_frames(result),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
