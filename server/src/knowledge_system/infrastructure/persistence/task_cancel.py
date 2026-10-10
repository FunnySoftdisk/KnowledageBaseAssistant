"""Task取消写集的原子事务执行器（开放态→CANCEL_REQUESTED、落事件、信号Outbox与审计）。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from knowledge_system.modules.audit.public import AuditService
from knowledge_system.modules.tasking.domain.cancel_decision import (
    CancelDisposition,
    decide_cancel,
)

from .repositories import TaskCancellationWriteSet, TaskNotFoundError
from .unit_of_work import SqlAlchemyUnitOfWork


class TaskCancelDisposition(StrEnum):
    REQUESTED = "REQUESTED"
    ALREADY_REQUESTED = "ALREADY_REQUESTED"
    ALREADY_CANCELLED = "ALREADY_CANCELLED"


class TaskCancelTerminalError(RuntimeError):
    """取消已进入 COMPLETED/FAILED 的终态任务；不得回退终态。"""


class TaskCancelConflictError(RuntimeError):
    """持锁重判后仍未命中开放态（并发下状态漂移）。"""


@dataclass(frozen=True, slots=True)
class TaskCancellationTransactionResult:
    disposition: TaskCancelDisposition
    task_id: UUID
    status: str


class TaskCancellationTransactionService:
    """开放态→CANCEL_REQUESTED 的单一事务；`FOR UPDATE` 持锁重读，幂等/终态在锁内重判。"""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], SqlAlchemyUnitOfWork],
        audit: AuditService,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._audit = audit

    async def execute(
        self, write_set: TaskCancellationWriteSet
    ) -> TaskCancellationTransactionResult:
        write_set.validate_relations()
        async with self._unit_of_work_factory() as unit_of_work:
            record = await unit_of_work.tasks.get_task(write_set.task_id, for_update=True)
            if record is None:
                raise TaskNotFoundError("TASK_NOT_FOUND")

            # 持锁重读当前状态并重新判定：应用层读取可能已过期（并发取消/终态）。
            disposition = decide_cancel(record.status)
            if disposition is CancelDisposition.ALREADY_CANCELLED:
                return TaskCancellationTransactionResult(
                    TaskCancelDisposition.ALREADY_CANCELLED, write_set.task_id, "CANCELLED"
                )
            if disposition is CancelDisposition.ALREADY_REQUESTED:
                return TaskCancellationTransactionResult(
                    TaskCancelDisposition.ALREADY_REQUESTED,
                    write_set.task_id,
                    "CANCEL_REQUESTED",
                )
            if disposition is CancelDisposition.TERMINAL:
                raise TaskCancelTerminalError("TASK_CANCEL_TERMINAL")

            # REQUESTED：开放态→CANCEL_REQUESTED 的单行 CAS，随后落事件/Outbox/审计。
            cas_ok = await unit_of_work.tasks.cas_cancel_request(write_set.task_id, record.status)
            if not cas_ok:
                raise TaskCancelConflictError("TASK_CANCEL_CAS_MISS")
            await unit_of_work.tasks.add_task_cancellation(write_set)
            await unit_of_work.tasks.flush()
            await self._audit.append(write_set.audit_event, unit_of_work)
            await unit_of_work.commit()

        return TaskCancellationTransactionResult(
            TaskCancelDisposition.REQUESTED, write_set.task_id, "CANCEL_REQUESTED"
        )


__all__ = [
    "TaskCancelConflictError",
    "TaskCancelDisposition",
    "TaskCancelTerminalError",
    "TaskCancellationTransactionResult",
    "TaskCancellationTransactionService",
]
