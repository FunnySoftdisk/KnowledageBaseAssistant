"""任务入口路由：创建（幂等）、详情与当前结果查询。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from knowledge_system.modules.iam.public import Subject
from knowledge_system.modules.tasking.public import (
    CreateTaskResult,
    TaskCreationService,
    TaskDetailView,
    TaskQueryService,
    TaskResultView,
    parse_create_task_request,
)

from ..dependencies import get_subject, get_task_creation_service, get_task_query_service
from ..errors import RequestParseError, pydantic_field_errors
from ..schemas import TaskCreateResponse

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


@router.get("/{task_id}", response_model=TaskDetailView)
async def get_task(
    task_id: UUID,
    subject: Annotated[Subject, Depends(get_subject)],
    service: Annotated[TaskQueryService, Depends(get_task_query_service)],
) -> TaskDetailView:
    return await service.get_detail(task_id, subject)
