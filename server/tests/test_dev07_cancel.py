"""DEV-07 取消请求的离线契约：纯领域判定与应用服务编排。

不依赖运行中的 FastAPI/PostgreSQL/Temporal，验证：
- `decide_cancel` 对十二状态的确定性折叠（终态拒绝、已取消/已请求幂等、开放态可请求）；
- 取消写集映射（TASK_CANCELLED 事件、TEMPORAL_SIGNAL Outbox、TASK_CANCEL 审计）；
- 所有权隔离（缺失与跨所有者同码 TASK_NOT_FOUND）；
- 终态拒绝（COMPLETED/FAILED → TASK_CANCEL_TERMINAL）；
- 事务内持锁重判竞态映射回稳定 409。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4

from knowledge_system.infrastructure.persistence import (
    TaskCancelDisposition,
    TaskCancellationTransactionResult,
    TaskCancelTerminalError,
    TaskNotFoundError,
)
from knowledge_system.modules.iam.domain.subject import Subject
from knowledge_system.modules.tasking.application.errors import TaskEntryError
from knowledge_system.modules.tasking.application.gateway import TaskRowSnapshot
from knowledge_system.modules.tasking.application.task_cancel import (
    TaskCancellationService,
)
from knowledge_system.modules.tasking.domain.cancel_decision import (
    CancelDisposition,
    decide_cancel,
)

_TASK_ID = UUID("5f0a7e8c-3d1b-4a2f-9c6e-1b2d3e4f5a6b")


def make_subject(**overrides: object) -> Subject:
    base = dict(
        user_id=uuid4(),
        session_id=uuid4(),
        organization_id=uuid4(),
        credential_version=1,
    )
    base.update(overrides)
    return Subject(**base)


def make_row(status: str, **overrides: object) -> TaskRowSnapshot:
    base = dict(
        task_id=_TASK_ID,
        owner_id=uuid4(),
        organization_id=uuid4(),
        status=status,
        conversation_id=None,
        deadline_at=datetime.now(UTC),
        active_plan_version=None,
        error_code=None,
        result_artifact_id=None,
        current_input_id=uuid4(),
        version=1,
        last_event_sequence=1,
        rerun_of_task_id=None,
        created_at=datetime.now(UTC),
        started_at=None,
        completed_at=None,
        updated_at=datetime.now(UTC),
    )
    base.update(overrides)
    return TaskRowSnapshot(**base)


class FakeTaskQueryGateway:
    def __init__(self, row: TaskRowSnapshot | None) -> None:
        self._row = row

    async def get_task(self, task_id: UUID) -> TaskRowSnapshot | None:
        return self._row


class FakeCancellationTransactionService:
    def __init__(self, *, terminal: bool = False, not_found: bool = False) -> None:
        self._terminal = terminal
        self._not_found = not_found
        self.write_set: object | None = None

    async def execute(self, write_set: object) -> TaskCancellationTransactionResult:
        self.write_set = write_set
        if self._not_found:
            raise TaskNotFoundError("TASK_NOT_FOUND")
        if self._terminal:
            raise TaskCancelTerminalError("TASK_CANCEL_TERMINAL")
        return TaskCancellationTransactionResult(
            disposition=TaskCancelDisposition.REQUESTED,
            task_id=write_set.task_id,  # type: ignore[attr-defined]
            status="CANCEL_REQUESTED",
        )


class DecideCancelTest(unittest.TestCase):
    def test_cancelled_is_already_cancelled(self) -> None:
        self.assertIs(decide_cancel("CANCELLED"), CancelDisposition.ALREADY_CANCELLED)

    def test_cancel_requested_is_already_requested(self) -> None:
        self.assertIs(decide_cancel("CANCEL_REQUESTED"), CancelDisposition.ALREADY_REQUESTED)

    def test_open_statuses_are_requested(self) -> None:
        for status in (
            "QUEUED",
            "PLANNING",
            "RUNNING",
            "REPLANNING",
            "FINALIZING",
            "WAITING_USER_INPUT",
            "WAITING_APPROVAL",
            "RETRY_WAIT",
        ):
            with self.subTest(status=status):
                self.assertIs(decide_cancel(status), CancelDisposition.REQUESTED)

    def test_terminal_statuses_are_terminal(self) -> None:
        for status in ("COMPLETED", "FAILED"):
            with self.subTest(status=status):
                self.assertIs(decide_cancel(status), CancelDisposition.TERMINAL)

    def test_unknown_status_fails_closed_as_terminal(self) -> None:
        self.assertIs(decide_cancel("SOMETHING_ELSE"), CancelDisposition.TERMINAL)


class TaskCancellationServiceTests(unittest.IsolatedAsyncioTestCase):
    def _service(
        self,
        subject: Subject,
        row: TaskRowSnapshot | None,
        *,
        terminal: bool = False,
    ) -> tuple[TaskCancellationService, FakeCancellationTransactionService]:
        transaction = FakeCancellationTransactionService(terminal=terminal)
        return TaskCancellationService(transaction, FakeTaskQueryGateway(row)), transaction

    async def test_open_owned_task_maps_to_cancel_write_set(self) -> None:
        subject = make_subject()
        row = make_row("RUNNING", owner_id=subject.user_id)
        service, transaction = self._service(subject, row)

        result = await service.cancel(_TASK_ID, subject)

        self.assertEqual(result.status, "CANCEL_REQUESTED")
        self.assertEqual(result.disposition, "REQUESTED")
        write_set = transaction.write_set  # type: ignore[attr-defined]
        self.assertEqual(write_set.cancelled_event.event_type, "TASK_CANCELLED")
        self.assertEqual(write_set.cancelled_event.business_ref_type, "TASK")
        self.assertEqual(write_set.cancelled_event.business_ref_id, _TASK_ID)
        self.assertEqual(
            write_set.cancelled_event.payload_json,
            {"task_id": str(_TASK_ID), "status": "CANCEL_REQUESTED"},
        )
        self.assertEqual(write_set.signal_outbox.destination, "TEMPORAL_SIGNAL")
        self.assertEqual(write_set.signal_outbox.event_id, write_set.cancelled_event.id)
        self.assertEqual(write_set.audit_event.action, "TASK_CANCEL")
        self.assertEqual(write_set.audit_event.resource_id, str(_TASK_ID))
        self.assertEqual(write_set.audit_event.result, "SUCCESS")

    async def test_missing_task_is_not_found(self) -> None:
        subject = make_subject()
        service, transaction = self._service(subject, None)
        with self.assertRaises(TaskEntryError) as caught:
            await service.cancel(_TASK_ID, subject)
        self.assertEqual(caught.exception.code, "TASK_NOT_FOUND")
        self.assertIsNone(transaction.write_set)

    async def test_cross_owner_is_not_found(self) -> None:
        subject = make_subject()
        row = make_row("RUNNING", owner_id=uuid4())
        service, transaction = self._service(subject, row)
        with self.assertRaises(TaskEntryError) as caught:
            await service.cancel(_TASK_ID, subject)
        self.assertEqual(caught.exception.code, "TASK_NOT_FOUND")
        self.assertIsNone(transaction.write_set)

    async def test_cancelled_is_idempotent_no_write(self) -> None:
        subject = make_subject()
        row = make_row("CANCELLED", owner_id=subject.user_id)
        service, transaction = self._service(subject, row)

        result = await service.cancel(_TASK_ID, subject)

        self.assertEqual(result.status, "CANCELLED")
        self.assertEqual(result.disposition, "ALREADY_CANCELLED")
        self.assertIsNone(transaction.write_set)

    async def test_cancel_requested_is_idempotent_no_write(self) -> None:
        subject = make_subject()
        row = make_row("CANCEL_REQUESTED", owner_id=subject.user_id)
        service, transaction = self._service(subject, row)

        result = await service.cancel(_TASK_ID, subject)

        self.assertEqual(result.status, "CANCEL_REQUESTED")
        self.assertEqual(result.disposition, "ALREADY_REQUESTED")
        self.assertIsNone(transaction.write_set)

    async def test_terminal_status_is_rejected(self) -> None:
        subject = make_subject()
        for status in ("COMPLETED", "FAILED"):
            with self.subTest(status=status):
                row = make_row(status, owner_id=subject.user_id)
                service, transaction = self._service(subject, row)
                with self.assertRaises(TaskEntryError) as caught:
                    await service.cancel(_TASK_ID, subject)
                self.assertEqual(caught.exception.code, "TASK_CANCEL_TERMINAL")
                self.assertIsNone(transaction.write_set)

    async def test_transaction_terminal_race_maps_to_terminal(self) -> None:
        subject = make_subject()
        row = make_row("RUNNING", owner_id=subject.user_id)
        service, _ = self._service(subject, row, terminal=True)
        with self.assertRaises(TaskEntryError) as caught:
            await service.cancel(_TASK_ID, subject)
        self.assertEqual(caught.exception.code, "TASK_CANCEL_TERMINAL")


if __name__ == "__main__":
    unittest.main()
