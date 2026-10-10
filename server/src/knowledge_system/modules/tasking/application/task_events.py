"""DEV-07：Task 事件流重连快照——REST 读事实、SSE 授权投影续读的应用编排。

把上一片固化的纯领域契约（`event_stream.py`）接到 `TaskQueryGateway`：给定 `Last-Event-ID`
游标，先做所有权隔离与终态判定（终态短路、不读事件），开放态才按 `sequence > cursor` 读取
`task_event` 窗口并做授权投影。本服务不持有长连接、不轮询；SSE 传输与心跳由 API 层完成。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from knowledge_system.modules.iam.domain.subject import Subject

from ..domain.event_stream import (
    ReconnectDecisionV1,
    RecoverActionV1,
    TaskEventProjectionV1,
    TaskStatusV1,
    decide_reconnect,
    parse_event_cursor,
    project_stream_event,
)
from .errors import TaskEntryError
from .gateway import TaskQueryGateway, TaskRowSnapshot

# 单次重连快照最多重放的事件窗口；超出部分由后续 `retry` 重连续读。
SSE_REPLAY_LIMIT = 256


class TaskEventReconnectResultV1(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    task_id: UUID
    status: TaskStatusV1
    decision: ReconnectDecisionV1
    events: tuple[TaskEventProjectionV1, ...] = ()


class TaskEventStreamService:
    """跨主体读取折叠为 TASK_NOT_FOUND；游标/投影违规由领域契约失败关闭。"""

    def __init__(self, gateway: TaskQueryGateway) -> None:
        self._gateway = gateway

    async def reconnect(
        self, task_id: UUID, subject: Subject, last_event_id: str | None
    ) -> TaskEventReconnectResultV1:
        row = await self._load_owned(task_id, subject)
        cursor = parse_event_cursor(last_event_id)
        status = TaskStatusV1(row.status)
        decision = decide_reconnect(
            status=status,
            server_last_event_sequence=row.last_event_sequence,
            cursor=cursor,
        )
        events: tuple[TaskEventProjectionV1, ...] = ()
        if decision.action is RecoverActionV1.STREAM_RESUME:
            snapshots = await self._gateway.list_events(
                task_id, after_sequence=cursor.sequence, limit=SSE_REPLAY_LIMIT
            )
            projected: list[TaskEventProjectionV1] = []
            for snapshot in snapshots:
                projection = project_stream_event(
                    sequence=snapshot.sequence,
                    event_type=snapshot.event_type,
                    event_schema_version=snapshot.event_schema_version,
                    payload=snapshot.payload,
                )
                if projection is not None:
                    projected.append(projection)
            events = tuple(projected)
        return TaskEventReconnectResultV1(
            task_id=task_id,
            status=status,
            decision=decision,
            events=events,
        )

    async def _load_owned(self, task_id: UUID, subject: Subject) -> TaskRowSnapshot:
        row = await self._gateway.get_task(task_id)
        if row is None:
            raise TaskEntryError("TASK_NOT_FOUND")
        if row.owner_id != subject.user_id or row.organization_id != subject.organization_id:
            raise TaskEntryError("TASK_NOT_FOUND")
        return row


__all__ = [
    "SSE_REPLAY_LIMIT",
    "TaskEventReconnectResultV1",
    "TaskEventStreamService",
]
