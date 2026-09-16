"""Task创建写集的幂等原子事务执行器。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from .repositories import TaskCreationWriteSet
from .task_models import IdempotencyRecord
from .unit_of_work import SqlAlchemyUnitOfWork


class TaskCreationDisposition(StrEnum):
    CREATED = "CREATED"
    REPLAYED = "REPLAYED"


class IdempotencyKeyReusedError(RuntimeError):
    """同一幂等键对应不同请求摘要。"""


class TaskCreationWriteConflictError(RuntimeError):
    """不是幂等重放的创建事务唯一约束冲突。"""


@dataclass(frozen=True, slots=True)
class TaskCreationTransactionResult:
    disposition: TaskCreationDisposition
    task_id: UUID
    response_code: int
    response_body: Mapping[str, object]
    response_digest: str


@dataclass(frozen=True, slots=True)
class _IdempotencySnapshot:
    """在Session关闭前提取的幂等结果，禁止向应用层泄漏ORM生命周期。"""

    request_hash: str
    status: str
    resource_id: UUID | None
    response_code: int | None
    response_body: Mapping[str, object] | None
    response_digest: str | None


class TaskCreationTransactionService:
    """提交Task/Message/Input/Event/Outbox/Idempotency的单一事务。"""

    def __init__(self, unit_of_work_factory: Callable[[], SqlAlchemyUnitOfWork]) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(self, write_set: TaskCreationWriteSet) -> TaskCreationTransactionResult:
        write_set.validate_relations()
        existing = await self._find_idempotency(write_set.idempotency)
        if existing is not None:
            return self._replay(existing, write_set.idempotency.request_hash)

        try:
            async with self._unit_of_work_factory() as unit_of_work:
                await unit_of_work.tasks.add_task_creation(write_set)
                await unit_of_work.tasks.flush()
                await unit_of_work.commit()
        except IntegrityError as error:
            # 并发首写的败者必须回读已提交幂等行；不能盲目当成重放。
            concurrent = await self._find_idempotency(write_set.idempotency)
            if concurrent is None:
                raise TaskCreationWriteConflictError("TASK_CREATION_WRITE_CONFLICT") from error
            return self._replay(concurrent, write_set.idempotency.request_hash)

        return self._result(
            self._snapshot(write_set.idempotency),
            TaskCreationDisposition.CREATED,
        )

    async def _find_idempotency(
        self,
        expected: IdempotencyRecord,
    ) -> _IdempotencySnapshot | None:
        async with self._unit_of_work_factory() as unit_of_work:
            record = await unit_of_work.tasks.get_idempotency(
                scope=expected.scope,
                actor_id=expected.actor_id,
                key_digest=expected.key_digest,
                for_update=True,
            )
            return None if record is None else self._snapshot(record)

    def _replay(
        self,
        existing: _IdempotencySnapshot,
        request_hash: str,
    ) -> TaskCreationTransactionResult:
        if existing.request_hash != request_hash:
            raise IdempotencyKeyReusedError("IDEMPOTENCY_KEY_REUSED")
        if existing.status != "COMPLETED":
            raise TaskCreationWriteConflictError("IDEMPOTENCY_RECORD_NOT_COMPLETED")
        return self._result(existing, TaskCreationDisposition.REPLAYED)

    @staticmethod
    def _result(
        record: _IdempotencySnapshot,
        disposition: TaskCreationDisposition,
    ) -> TaskCreationTransactionResult:
        if (
            record.resource_id is None
            or record.response_code is None
            or record.response_body is None
            or record.response_digest is None
        ):
            raise TaskCreationWriteConflictError("IDEMPOTENCY_RESPONSE_INCOMPLETE")
        return TaskCreationTransactionResult(
            disposition=disposition,
            task_id=record.resource_id,
            response_code=record.response_code,
            response_body=dict(record.response_body),
            response_digest=record.response_digest,
        )

    @staticmethod
    def _snapshot(record: IdempotencyRecord) -> _IdempotencySnapshot:
        response_body = (
            None if record.response_body_json is None else dict(record.response_body_json)
        )
        return _IdempotencySnapshot(
            request_hash=record.request_hash,
            status=record.status,
            resource_id=record.resource_id,
            response_code=record.response_code,
            response_body=response_body,
            response_digest=record.response_digest,
        )
