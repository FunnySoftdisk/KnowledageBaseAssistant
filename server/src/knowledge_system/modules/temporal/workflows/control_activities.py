"""DEV-03B：Root控制面Activity（PG事件游标推进与终态CAS）。

认知Activity（模型/工具）不属于本批；可靠执行骨架只实现事件游标推进与终态CAS。
这些Activity在Worker的control队列执行，禁止继承Workflow Queue；它们运行在Workflow
沙箱之外，可以自由访问SQLAlchemy/psycopg。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from knowledge_system.infrastructure.persistence.task_models import (
    IntelligentTaskRecord,
    TaskEventRecord,
)
from knowledge_system.modules.temporal.domain.contracts import (
    ApplyBusinessEventsCommandV1,
    ApplyBusinessEventsResultV1,
    RootTerminalCommitV1,
)

APPLY_BUSINESS_EVENTS_ACTIVITY = "apply_business_events_v1"
COMMIT_ROOT_TERMINAL_STATE_ACTIVITY = "commit_root_terminal_state_v1"

_TERMINAL_EVENT_TYPES = frozenset({"TASK_FAILED", "TASK_CANCELLED"})

# 终态Task不可回退；此集合是终态CAS的唯一放行条件。
_OPEN_STATUSES = (
    "QUEUED",
    "PLANNING",
    "RUNNING",
    "REPLANNING",
    "FINALIZING",
    "WAITING_USER_INPUT",
    "WAITING_APPROVAL",
    "RETRY_WAIT",
    "CANCEL_REQUESTED",
)


def _terminal_state_for(event_type: str) -> Literal["FAILED", "CANCELLED"] | None:
    if event_type == "TASK_CANCELLED":
        return "CANCELLED"
    if event_type == "TASK_FAILED":
        return "FAILED"
    return None


class ControlActivities:
    """Root控制Activity；依赖以构造注入，不直接持有全局Session。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @activity.defn(name=APPLY_BUSINESS_EVENTS_ACTIVITY)
    async def apply_business_events(
        self, command: ApplyBusinessEventsCommandV1
    ) -> ApplyBusinessEventsResultV1:
        """按`from_sequence`（不含）读取并推进业务事件游标。

        本批只推进游标并识别终态事件；事件到状态机/Attempt调度的语义在DEV-03C+展开。
        """

        async with self._session_factory() as session:
            statement = (
                select(TaskEventRecord)
                .where(
                    TaskEventRecord.task_id == command.task_id,
                    TaskEventRecord.sequence > command.from_sequence,
                )
                .order_by(TaskEventRecord.sequence)
            )
            events = (await session.scalars(statement)).all()
            new_cursor = command.from_sequence
            terminal_requested: Literal["FAILED", "CANCELLED"] | None = None
            for event in events:
                new_cursor = max(new_cursor, event.sequence)
                if event.event_type in _TERMINAL_EVENT_TYPES:
                    terminal_requested = _terminal_state_for(event.event_type) or terminal_requested
            return ApplyBusinessEventsResultV1(
                task_id=command.task_id,
                applied_count=len(events),
                new_cursor=new_cursor,
                terminal_requested=terminal_requested,
            )

    @activity.defn(name=COMMIT_ROOT_TERMINAL_STATE_ACTIVITY)
    async def commit_root_terminal_state(self, commit: RootTerminalCommitV1) -> None:
        """终态CAS：仅当Task仍处于开放状态时写入FAILED/CANCELLED，幂等收敛。"""

        async with self._session_factory() as session:
            await session.execute(
                update(IntelligentTaskRecord)
                .where(
                    IntelligentTaskRecord.id == commit.task_id,
                    IntelligentTaskRecord.status.in_(_OPEN_STATUSES),
                )
                .values(
                    status=commit.terminal_state,
                    error_code=commit.error_code,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()


__all__ = [
    "APPLY_BUSINESS_EVENTS_ACTIVITY",
    "COMMIT_ROOT_TERMINAL_STATE_ACTIVITY",
    "ControlActivities",
]
