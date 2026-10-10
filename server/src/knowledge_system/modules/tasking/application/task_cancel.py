"""DEV-07：取消请求的应用编排——所有权隔离、终态/幂等早判与取消写集执行。

终态不可回退、取消请求幂等，判定依据 `cancel_decision.decide_cancel`。真实写事务在
`TaskCancellationTransactionService` 内 `FOR UPDATE` 持锁重读并重判，应用层早判只是
为了免事务的幂等/终态快速返回；两者之间的竞态由事务内重判兜底（失败关闭）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from knowledge_system.infrastructure.persistence import (
    TaskCancellationTransactionService,
    TaskCancellationWriteSet,
    TaskCancelTerminalError,
    TaskNotFoundError,
)
from knowledge_system.infrastructure.persistence.task_models import (
    OutboxMessageRecord,
    TaskEventRecord,
)
from knowledge_system.modules.audit.public import AuditEventDraft, AuditResult
from knowledge_system.modules.iam.domain.subject import Subject
from knowledge_system.modules.tasking.domain.cancel_decision import (
    CancelDisposition,
    decide_cancel,
)
from knowledge_system.modules.tasking.domain.core_types import canonical_json_sha256

from .errors import TaskEntryError
from .gateway import TaskQueryGateway

# TEMPORAL_SIGNAL 发件行的载荷契约：`business_event_available_v1` 信号本身无参，
# 发布器仅需 task_id（解析 workflow_id）与 event_id（追踪）。
CANCEL_SIGNAL_SCHEMA_VERSION: Final = "agent_task_workflow_signal_v1"
TASK_CANCELLED_EVENT_SCHEMA_VERSION: Final = "task_event_v1"


@dataclass(frozen=True, slots=True)
class TaskCancelResult:
    task_id: UUID
    status: str
    disposition: str


class TaskCancellationService:
    """请求→取消写集→事务的编排；读取端口与写事务均注入，不直接持有Session。"""

    def __init__(
        self,
        transaction_service: TaskCancellationTransactionService,
        query: TaskQueryGateway,
    ) -> None:
        self._transaction_service = transaction_service
        self._query = query

    async def cancel(
        self,
        task_id: UUID,
        subject: Subject,
        *,
        trace_id: str | None = None,
    ) -> TaskCancelResult:
        row = await self._query.get_task(task_id)
        # 跨所有者与不存在同码（TASK_NOT_FOUND）：不向无权主体泄露任务存在性。
        if row is None or row.owner_id != subject.user_id:
            raise TaskEntryError("TASK_NOT_FOUND")

        disposition = decide_cancel(row.status)
        if disposition is CancelDisposition.ALREADY_CANCELLED:
            return TaskCancelResult(task_id, "CANCELLED", "ALREADY_CANCELLED")
        if disposition is CancelDisposition.ALREADY_REQUESTED:
            return TaskCancelResult(task_id, "CANCEL_REQUESTED", "ALREADY_REQUESTED")
        if disposition is CancelDisposition.TERMINAL:
            raise TaskEntryError("TASK_CANCEL_TERMINAL")

        write_set = self._build_write_set(task_id, subject, trace_id)
        try:
            result = await self._transaction_service.execute(write_set)
        except TaskCancelTerminalError as error:
            # 应用层早判与事务持锁重判之间的竞态：终态已收敛，稳定拒绝。
            raise TaskEntryError("TASK_CANCEL_TERMINAL") from error
        except TaskNotFoundError as error:
            raise TaskEntryError("TASK_NOT_FOUND") from error

        return TaskCancelResult(task_id, result.status, result.disposition.value)

    @staticmethod
    def _build_write_set(
        task_id: UUID, subject: Subject, trace_id: str | None
    ) -> TaskCancellationWriteSet:
        event_id = uuid4()
        outbox_id = uuid4()
        audit_event_id = uuid4()
        now = datetime.now(UTC)
        payload: dict[str, object] = {"task_id": str(task_id), "status": "CANCEL_REQUESTED"}
        outbox_payload: dict[str, object] = {"task_id": str(task_id), "event_id": str(event_id)}

        # `sequence` 与 `business_version` 由事务内 `allocate_next_event_sequence` 权威回填，
        # 应用层不预计算序号以避免并发追加错位。
        event = TaskEventRecord(
            id=event_id,
            task_id=task_id,
            sequence=0,
            event_type="TASK_CANCELLED",
            event_schema_version=TASK_CANCELLED_EVENT_SCHEMA_VERSION,
            workflow_relevant=True,
            business_ref_type="TASK",
            business_ref_id=task_id,
            business_version=None,
            observation_expected_count=0,
            payload_json=payload,
            payload_digest=canonical_json_sha256(payload),
        )
        outbox = OutboxMessageRecord(
            id=outbox_id,
            event_id=event_id,
            aggregate_type="TASK",
            aggregate_id=task_id,
            aggregate_version=0,
            destination="TEMPORAL_SIGNAL",
            schema_version=CANCEL_SIGNAL_SCHEMA_VERSION,
            payload_json=outbox_payload,
            payload_digest=canonical_json_sha256(outbox_payload),
            publish_status="PENDING",
            available_at=now,
            attempt_count=0,
            row_version=1,
        )
        audit_event = AuditEventDraft(
            event_id=audit_event_id,
            occurred_at=now,
            actor_id=subject.user_id,
            actor_role_snapshot=(),
            session_id=subject.session_id,
            source_ip=None,
            device_id=None,
            action="TASK_CANCEL",
            resource_type="TASK",
            resource_id=str(task_id),
            result=AuditResult.SUCCESS.value,
            reason_code=None,
            before_digest=None,
            after_digest=None,
            details_json=None,
            trace_id=trace_id or uuid4().hex,
        )
        return TaskCancellationWriteSet(
            task_id=task_id,
            cancelled_event=event,
            signal_outbox=outbox,
            audit_event=audit_event,
        )


__all__ = [
    "TaskCancelResult",
    "TaskCancellationService",
]
