"""任务入口读取端口：把认证主体与对话/附件/任务查询从ORM解耦。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ConversationEntrySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    conversation_id: UUID
    organization_id: UUID
    next_message_sequence: int = Field(ge=1)


class AttachmentEntrySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    artifact_id: UUID
    organization_id: UUID
    artifact_type: str
    sha256: str
    schema_id: str
    schema_version: str
    detected_media_type: str
    purpose: str
    state: str


class TaskEntryGateway(Protocol):
    async def load_conversation(
        self, conversation_id: UUID, owner_id: UUID
    ) -> ConversationEntrySnapshot | None: ...

    async def load_attachment(
        self, artifact_id: UUID, owner_id: UUID
    ) -> AttachmentEntrySnapshot | None: ...


class TaskRowSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    task_id: UUID
    owner_id: UUID
    organization_id: UUID
    status: str
    conversation_id: UUID | None
    deadline_at: datetime
    active_plan_version: int | None
    error_code: str | None
    result_artifact_id: UUID | None
    current_input_id: UUID
    version: int
    last_event_sequence: int
    rerun_of_task_id: UUID | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime


class TaskQueryGateway(Protocol):
    async def get_task(self, task_id: UUID) -> TaskRowSnapshot | None: ...
