"""Task事务的SQLAlchemy Repository；本模块永不提交事务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .foundation_models import ArtifactRecord
from .task_models import (
    ConversationMessageRecord,
    IdempotencyRecord,
    IntelligentTaskRecord,
    OutboxMessageRecord,
    TaskAttachmentBindingRecord,
    TaskEventRecord,
    TaskInputSnapshotRecord,
)

TASK_CREATED_EVENT_SEQUENCE: Final = 1


class PersistenceContractError(RuntimeError):
    """持久化写集违反已冻结关系。"""


class TaskNotFoundError(LookupError):
    """事件序号分配时Task不存在。"""


@dataclass(frozen=True, slots=True)
class TaskCreationWriteSet:
    """`POST /tasks`需要原子保存的已映射行集。"""

    task: IntelligentTaskRecord
    query_message: ConversationMessageRecord
    initial_input: TaskInputSnapshotRecord
    attachment_bindings: tuple[TaskAttachmentBindingRecord, ...]
    task_created_event: TaskEventRecord
    starter_outbox: OutboxMessageRecord
    idempotency: IdempotencyRecord
    # USER_QUERY正文Artifact与Task/Message同事务写入，避免失败后留下孤立Artifact。
    query_artifact: ArtifactRecord | None = None

    def validate_relations(self) -> None:
        task_id = self.task.id
        input_id = self.initial_input.input_id
        event_id = self.task_created_event.id
        if self.task.status != "QUEUED":
            raise PersistenceContractError("TASK_INITIAL_STATUS_NOT_QUEUED")
        if self.task.last_event_sequence != 0:
            raise PersistenceContractError("TASK_INITIAL_EVENT_SEQUENCE_NOT_ZERO")
        if self.task.initial_input_id != input_id or self.task.current_input_id != input_id:
            raise PersistenceContractError("TASK_INITIAL_INPUT_RELATION_INVALID")
        if self.query_message.task_id != task_id or self.initial_input.task_id != task_id:
            raise PersistenceContractError("TASK_WRITE_SET_TASK_MISMATCH")
        if self.query_artifact is not None:
            if self.query_artifact.artifact_id != self.task.query_artifact_id:
                raise PersistenceContractError("TASK_QUERY_ARTIFACT_MISMATCH")
            if self.query_artifact.artifact_id != self.query_message.content_artifact_id:
                raise PersistenceContractError("TASK_QUERY_MESSAGE_ARTIFACT_MISMATCH")
        if self.initial_input.message_id != self.query_message.id:
            raise PersistenceContractError("TASK_INPUT_MESSAGE_MISMATCH")
        if self.initial_input.input_revision != 1:
            raise PersistenceContractError("TASK_INITIAL_INPUT_REVISION_INVALID")
        if self.query_message.message_kind != "USER_QUERY" or self.query_message.state != "ACTIVE":
            raise PersistenceContractError("TASK_QUERY_MESSAGE_INVALID")
        if any(binding.task_id != task_id for binding in self.attachment_bindings):
            raise PersistenceContractError("TASK_ATTACHMENT_BINDING_TASK_MISMATCH")
        if self.task_created_event.task_id != task_id:
            raise PersistenceContractError("TASK_EVENT_TASK_MISMATCH")
        if self.task_created_event.sequence != TASK_CREATED_EVENT_SEQUENCE:
            raise PersistenceContractError("TASK_CREATED_EVENT_SEQUENCE_INVALID")
        if self.starter_outbox.event_id != event_id:
            raise PersistenceContractError("TASK_OUTBOX_EVENT_MISMATCH")
        if self.idempotency.status != "COMPLETED":
            raise PersistenceContractError("TASK_IDEMPOTENCY_NOT_COMPLETED")
        if self.idempotency.resource_type != "TASK" or self.idempotency.resource_id != task_id:
            raise PersistenceContractError("TASK_IDEMPOTENCY_RESOURCE_MISMATCH")


class TaskTransactionRepository:
    """Task行、事件、Outbox及幂等记录的同Session适配器。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_task_creation(self, write_set: TaskCreationWriteSet) -> None:
        write_set.validate_relations()
        # Query Artifact先行，Task/Message的content/query FK指向它。
        if write_set.query_artifact is not None:
            self._session.add(write_set.query_artifact)
            await self._session.flush((write_set.query_artifact,))
        self._session.add(write_set.task)
        # Task/Input是延迟外键；先插Task才能用权威UPDATE...RETURNING分配序号。
        await self._session.flush((write_set.task,))
        sequence = await self.allocate_next_event_sequence(write_set.task.id)
        if sequence != write_set.task_created_event.sequence:
            raise PersistenceContractError("TASK_CREATED_EVENT_SEQUENCE_ALLOCATION_MISMATCH")
        event_dependencies = (
            write_set.query_message,
            write_set.initial_input,
            *write_set.attachment_bindings,
            write_set.task_created_event,
        )
        self._session.add_all(event_dependencies)
        # 纯Mapper不建relationship，因此明确在Outbox之前落下Event。
        await self._session.flush(event_dependencies)
        self._session.add(write_set.starter_outbox)
        self._session.add(write_set.idempotency)

    async def allocate_next_event_sequence(self, task_id: UUID) -> int:
        statement = (
            update(IntelligentTaskRecord)
            .where(IntelligentTaskRecord.id == task_id)
            .values(last_event_sequence=IntelligentTaskRecord.last_event_sequence + 1)
            .returning(IntelligentTaskRecord.last_event_sequence)
        )
        sequence = await self._session.scalar(statement)
        if sequence is None:
            raise TaskNotFoundError("TASK_NOT_FOUND")
        return sequence

    async def get_task(
        self,
        task_id: UUID,
        *,
        for_update: bool = False,
    ) -> IntelligentTaskRecord | None:
        statement: Select[tuple[IntelligentTaskRecord]] = select(IntelligentTaskRecord).where(
            IntelligentTaskRecord.id == task_id
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.scalars(statement)
        return result.first()

    async def get_idempotency(
        self,
        *,
        scope: str,
        actor_id: UUID,
        key_digest: str,
        for_update: bool = False,
    ) -> IdempotencyRecord | None:
        statement: Select[tuple[IdempotencyRecord]] = select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.actor_id == actor_id,
            IdempotencyRecord.key_digest == key_digest,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.scalars(statement)
        return result.first()

    async def flush(self) -> None:
        """显式flush仅用于事务内立即校验；不提交。"""

        await self._session.flush()
