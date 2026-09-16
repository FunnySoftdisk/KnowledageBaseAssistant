"""Task入口/查询读取SQLAlchemy适配器；实现TaskEntryGateway与TaskQueryGateway端口。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_system.modules.tasking.application.gateway import (
    AttachmentEntrySnapshot,
    ConversationEntrySnapshot,
    TaskRowSnapshot,
)

from .foundation_models import ArtifactRecord, ConversationRecord
from .task_models import ConversationMessageRecord, IntelligentTaskRecord


class TaskReadRepository:
    """只读端口适配；序列号分配由服务在写入前读取，并发追加见DEV-03。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_conversation(
        self, conversation_id: UUID, owner_id: UUID
    ) -> ConversationEntrySnapshot | None:
        statement: Select[tuple[ConversationRecord]] = select(ConversationRecord).where(
            ConversationRecord.id == conversation_id,
            ConversationRecord.owner_id == owner_id,
        )
        record = (await self._session.scalars(statement)).first()
        if record is None:
            return None
        next_sequence = await self._next_message_sequence(conversation_id)
        return ConversationEntrySnapshot(
            conversation_id=record.id,
            organization_id=record.organization_id,
            next_message_sequence=next_sequence,
        )

    async def load_attachment(
        self, artifact_id: UUID, owner_id: UUID
    ) -> AttachmentEntrySnapshot | None:
        statement: Select[tuple[ArtifactRecord]] = select(ArtifactRecord).where(
            ArtifactRecord.artifact_id == artifact_id,
            ArtifactRecord.owner_id == owner_id,
        )
        record = (await self._session.scalars(statement)).first()
        if record is None:
            return None
        return AttachmentEntrySnapshot(
            artifact_id=record.artifact_id,
            organization_id=record.organization_id,
            artifact_type=record.artifact_type,
            sha256=record.sha256,
            schema_id=record.schema_id,
            schema_version=record.schema_version,
            detected_media_type=record.detected_media_type,
            purpose=record.purpose,
            state=record.state,
        )

    async def get_task(self, task_id: UUID) -> TaskRowSnapshot | None:
        statement: Select[tuple[IntelligentTaskRecord]] = select(IntelligentTaskRecord).where(
            IntelligentTaskRecord.id == task_id
        )
        record = (await self._session.scalars(statement)).first()
        if record is None:
            return None
        return TaskRowSnapshot(
            task_id=record.id,
            owner_id=record.owner_id,
            organization_id=record.organization_id,
            status=record.status,
            conversation_id=record.conversation_id,
            deadline_at=record.deadline_at,
            active_plan_version=record.active_plan_version,
            error_code=record.error_code,
            result_artifact_id=record.result_artifact_id,
            current_input_id=record.current_input_id,
            version=record.version,
            last_event_sequence=record.last_event_sequence,
            rerun_of_task_id=record.rerun_of_task_id,
            created_at=record.created_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            updated_at=record.updated_at,
        )

    async def _next_message_sequence(self, conversation_id: UUID) -> int:
        max_sequence = await self._session.scalar(
            select(func.max(ConversationMessageRecord.sequence)).where(
                ConversationMessageRecord.conversation_id == conversation_id
            )
        )
        return 1 if max_sequence is None else max_sequence + 1
