"""Task创建写集和幂等事务服务的纯逻辑测试。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from typing import Self
from uuid import uuid4

from knowledge_system.infrastructure.persistence import (
    IdempotencyKeyReusedError,
    PersistenceContractError,
    TaskCreationDisposition,
    TaskCreationTransactionService,
    TaskCreationWriteSet,
)
from knowledge_system.infrastructure.persistence.task_models import (
    ConversationMessageRecord,
    IdempotencyRecord,
    IntelligentTaskRecord,
    OutboxMessageRecord,
    TaskEventRecord,
    TaskInputSnapshotRecord,
)
from knowledge_system.modules.audit.domain.audit_event import AuditEventDraft, AuditResult
from knowledge_system.modules.audit.public import AuditService


def make_write_set(*, request_hash: str = "b" * 64) -> TaskCreationWriteSet:
    task_id = uuid4()
    input_id = uuid4()
    message_id = uuid4()
    event_id = uuid4()
    actor_id = uuid4()
    task = IntelligentTaskRecord(
        id=task_id,
        status="QUEUED",
        last_event_sequence=0,
        initial_input_id=input_id,
        current_input_id=input_id,
    )
    message = ConversationMessageRecord(
        id=message_id,
        task_id=task_id,
        message_kind="USER_QUERY",
        state="ACTIVE",
    )
    snapshot = TaskInputSnapshotRecord(
        input_id=input_id,
        task_id=task_id,
        message_id=message_id,
        input_revision=1,
    )
    event = TaskEventRecord(id=event_id, task_id=task_id, sequence=1)
    outbox = OutboxMessageRecord(event_id=event_id)
    idempotency = IdempotencyRecord(
        scope="CREATE_TASK",
        actor_id=actor_id,
        key_digest="a" * 64,
        request_hash=request_hash,
        status="COMPLETED",
        resource_type="TASK",
        resource_id=task_id,
        response_code=202,
        response_body_json={"task_id": str(task_id), "status": "QUEUED"},
        response_digest="c" * 64,
    )
    audit_event = AuditEventDraft(
        event_id=uuid4(),
        occurred_at=datetime.now(UTC),
        actor_id=actor_id,
        actor_role_snapshot=(),
        session_id=None,
        source_ip=None,
        device_id=None,
        action="TASK_CREATE",
        resource_type="TASK",
        resource_id=str(task_id),
        result=AuditResult.SUCCESS.value,
        reason_code=None,
        before_digest=None,
        after_digest=None,
        details_json=None,
        trace_id="a" * 32,
    )
    return TaskCreationWriteSet(
        task=task,
        query_message=message,
        initial_input=snapshot,
        attachment_bindings=(),
        task_created_event=event,
        starter_outbox=outbox,
        idempotency=idempotency,
        audit_event=audit_event,
    )


class FakeTaskRepository:
    def __init__(self, existing: IdempotencyRecord | None = None) -> None:
        self.existing = existing
        self.added = False
        self.flushed = False

    async def get_idempotency(self, **_: object) -> IdempotencyRecord | None:
        return self.existing

    async def add_task_creation(self, write_set: TaskCreationWriteSet) -> None:
        write_set.validate_relations()
        self.added = True

    async def flush(self) -> None:
        self.flushed = True


class FakeAuditRepository:
    def __init__(self) -> None:
        self.appended: list[AuditEventDraft] = []

    async def append(self, draft: AuditEventDraft) -> None:
        self.appended.append(draft)


class FakeUnitOfWork:
    def __init__(self, repository: FakeTaskRepository) -> None:
        self.tasks = repository
        self.audit = FakeAuditRepository()
        self.committed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class TaskCreationWriteSetTests(unittest.TestCase):
    def test_valid_write_set_is_accepted(self) -> None:
        make_write_set().validate_relations()

    def test_mismatched_current_input_fails_before_database_write(self) -> None:
        write_set = make_write_set()
        write_set.task.current_input_id = uuid4()
        with self.assertRaisesRegex(
            PersistenceContractError, "TASK_INITIAL_INPUT_RELATION_INVALID"
        ):
            write_set.validate_relations()

    def test_non_completed_idempotency_fails_closed(self) -> None:
        write_set = make_write_set()
        write_set.idempotency.status = "PROCESSING"
        with self.assertRaisesRegex(PersistenceContractError, "TASK_IDEMPOTENCY_NOT_COMPLETED"):
            write_set.validate_relations()


class TaskCreationTransactionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_write_set_is_flushed_and_explicitly_committed(self) -> None:
        write_set = make_write_set()
        repositories = [FakeTaskRepository(), FakeTaskRepository()]
        units: list[FakeUnitOfWork] = []

        def factory() -> FakeUnitOfWork:
            unit = FakeUnitOfWork(repositories[len(units)])
            units.append(unit)
            return unit

        service = TaskCreationTransactionService(factory, AuditService())  # type: ignore[arg-type]
        result = await service.execute(write_set)
        self.assertEqual(result.disposition, TaskCreationDisposition.CREATED)
        self.assertTrue(repositories[1].added)
        self.assertTrue(repositories[1].flushed)
        self.assertTrue(units[1].committed)
        self.assertEqual(len(units[1].audit.appended), 1)

    async def test_same_request_is_replayed_without_new_write(self) -> None:
        write_set = make_write_set()
        repository = FakeTaskRepository(write_set.idempotency)
        unit = FakeUnitOfWork(repository)
        service = TaskCreationTransactionService(lambda: unit, AuditService())  # type: ignore[arg-type]
        result = await service.execute(write_set)
        self.assertEqual(result.disposition, TaskCreationDisposition.REPLAYED)
        self.assertEqual(result.task_id, write_set.task.id)
        self.assertFalse(repository.added)
        self.assertFalse(unit.committed)

    async def test_same_key_with_different_request_hash_is_rejected(self) -> None:
        write_set = make_write_set(request_hash="b" * 64)
        existing = make_write_set(request_hash="d" * 64).idempotency
        existing.scope = write_set.idempotency.scope
        existing.actor_id = write_set.idempotency.actor_id
        existing.key_digest = write_set.idempotency.key_digest
        repository = FakeTaskRepository(existing)
        service = TaskCreationTransactionService(  # type: ignore[arg-type]
            lambda: FakeUnitOfWork(repository),
            AuditService(),
        )
        with self.assertRaisesRegex(IdempotencyKeyReusedError, "IDEMPOTENCY_KEY_REUSED"):
            await service.execute(write_set)


if __name__ == "__main__":
    unittest.main()
