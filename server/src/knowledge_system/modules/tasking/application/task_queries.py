"""CORE-17~25：Task详情与当前结果的只读查询，强制所有者/组织隔离。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from knowledge_system.modules.iam.domain.subject import Subject

from .errors import TaskEntryError
from .gateway import TaskQueryGateway, TaskRowSnapshot


class TaskDetailView(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    task_id: UUID
    status: str
    conversation_id: UUID | None
    deadline_at: datetime
    active_plan_version: int | None
    error_code: str | None
    result_artifact_id: UUID | None
    version: int
    last_event_sequence: int
    rerun_of_task_id: UUID | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime


class TaskResultView(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    task_id: UUID
    status: str
    result_artifact_id: UUID | None
    error_code: str | None
    completed_at: datetime | None


class TaskQueryService:
    """跨主体读取一律折叠为TASK_NOT_FOUND，不泄漏存在性。"""

    def __init__(self, gateway: TaskQueryGateway) -> None:
        self._gateway = gateway

    async def get_detail(self, task_id: UUID, subject: Subject) -> TaskDetailView:
        row = await self._load_owned(task_id, subject)
        return TaskDetailView(
            task_id=row.task_id,
            status=row.status,
            conversation_id=row.conversation_id,
            deadline_at=row.deadline_at,
            active_plan_version=row.active_plan_version,
            error_code=row.error_code,
            result_artifact_id=row.result_artifact_id,
            version=row.version,
            last_event_sequence=row.last_event_sequence,
            rerun_of_task_id=row.rerun_of_task_id,
            created_at=row.created_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
            updated_at=row.updated_at,
        )

    async def get_result(self, task_id: UUID, subject: Subject) -> TaskResultView:
        row = await self._load_owned(task_id, subject)
        return TaskResultView(
            task_id=row.task_id,
            status=row.status,
            result_artifact_id=row.result_artifact_id,
            error_code=row.error_code,
            completed_at=row.completed_at,
        )

    async def _load_owned(self, task_id: UUID, subject: Subject) -> TaskRowSnapshot:
        row = await self._gateway.get_task(task_id)
        if row is None:
            raise TaskEntryError("TASK_NOT_FOUND")
        if row.owner_id != subject.user_id or row.organization_id != subject.organization_id:
            raise TaskEntryError("TASK_NOT_FOUND")
        return row
