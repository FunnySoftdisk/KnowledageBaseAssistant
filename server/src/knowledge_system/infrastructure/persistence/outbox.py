"""Outbox读取/认领/发布状态持久化（TEMPORAL_START等异步出口）。

事务性发件箱的消费者侧：`FOR UPDATE SKIP LOCKED`原子认领，租约超时重领崩溃残留的
`PUBLISHING`行。发布结果（PUBLISHED/PENDING/DEAD）以独立短事务写回，遵守
`outbox_message`的`publish_state_shape` CHECK约束。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .task_models import IntelligentTaskRecord, OutboxMessageRecord, TaskEventRecord


@dataclass(frozen=True, slots=True)
class ClaimedOutboxMessage:
    """认领行在Session关闭前提取的投影，禁止向应用层泄漏ORM生命周期。"""

    id: UUID
    event_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    schema_version: str
    payload_json: dict[str, object]
    payload_digest: str
    attempt_count: int


@dataclass(frozen=True, slots=True)
class TaskStartContext:
    """构建`AgentTaskWorkflowStartV1`所需的权威Task/Event事实（来自PG）。"""

    deadline_at: datetime
    event_sequence: int


class OutboxStartContextMissingError(LookupError):
    """Task或其`TASK_CREATED`事件不存在，无法构建START参数。"""


class SqlAlchemyOutboxRepository:
    """消费者侧Outbox仓储；每个方法开启独立短事务并提交。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim_batch(
        self,
        *,
        destination: str,
        limit: int,
        lease_seconds: float,
        claim_id: str,
    ) -> list[ClaimedOutboxMessage]:
        """原子认领一批`PENDING`（或租约过期的`PUBLISHING`）行。"""

        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=lease_seconds)
        async with self._session_factory() as session:
            statement = (
                select(OutboxMessageRecord)
                .where(
                    OutboxMessageRecord.destination == destination,
                    or_(
                        and_(
                            OutboxMessageRecord.publish_status == "PENDING",
                            OutboxMessageRecord.available_at <= now,
                        ),
                        and_(
                            OutboxMessageRecord.publish_status == "PUBLISHING",
                            OutboxMessageRecord.claimed_at < stale_before,
                        ),
                    ),
                )
                .order_by(OutboxMessageRecord.available_at, OutboxMessageRecord.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
            rows = (await session.scalars(statement)).all()
            claimed: list[ClaimedOutboxMessage] = []
            for row in rows:
                row.publish_status = "PUBLISHING"
                row.claimed_by = claim_id
                row.claimed_at = now
                row.attempt_count = row.attempt_count + 1
                row.row_version = row.row_version + 1
                row.published_at = None
                row.last_error_code = None
                claimed.append(
                    ClaimedOutboxMessage(
                        id=row.id,
                        event_id=row.event_id,
                        aggregate_type=row.aggregate_type,
                        aggregate_id=row.aggregate_id,
                        aggregate_version=row.aggregate_version,
                        schema_version=row.schema_version,
                        payload_json=dict(row.payload_json),
                        payload_digest=row.payload_digest,
                        attempt_count=row.attempt_count,
                    )
                )
            await session.commit()
        return claimed

    async def load_start_context(self, task_id: UUID, event_id: UUID) -> TaskStartContext | None:
        async with self._session_factory() as session:
            task = (
                await session.scalars(
                    select(IntelligentTaskRecord).where(IntelligentTaskRecord.id == task_id)
                )
            ).first()
            event = (
                await session.scalars(
                    select(TaskEventRecord).where(
                        TaskEventRecord.id == event_id,
                        TaskEventRecord.task_id == task_id,
                        TaskEventRecord.event_type == "TASK_CREATED",
                        TaskEventRecord.workflow_relevant.is_(True),
                    )
                )
            ).first()
        if task is None or event is None:
            return None
        return TaskStartContext(deadline_at=task.deadline_at, event_sequence=event.sequence)

    async def mark_published(self, message: ClaimedOutboxMessage, claim_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(OutboxMessageRecord)
                .where(
                    OutboxMessageRecord.id == message.id,
                    OutboxMessageRecord.publish_status == "PUBLISHING",
                    OutboxMessageRecord.claimed_by == claim_id,
                )
                .values(
                    publish_status="PUBLISHED",
                    published_at=datetime.now(UTC),
                    claimed_by=None,
                    claimed_at=None,
                    row_version=OutboxMessageRecord.row_version + 1,
                )
            )
            await session.commit()

    async def mark_retry(
        self,
        message: ClaimedOutboxMessage,
        claim_id: str,
        error_code: str,
        *,
        max_attempts: int,
    ) -> Literal["PENDING", "DEAD"]:
        """发布失败：重试回到`PENDING`，超次置`DEAD`；两者均清空认领租约字段。"""

        dead = message.attempt_count >= max_attempts
        publish_status: Literal["PENDING", "DEAD"] = "DEAD" if dead else "PENDING"
        await self._mark_failed(message, claim_id, error_code, publish_status=publish_status)
        return publish_status

    async def mark_dead(
        self, message: ClaimedOutboxMessage, claim_id: str, error_code: str
    ) -> None:
        """不可恢复失败：直接置`DEAD`，不再重试。"""

        await self._mark_failed(message, claim_id, error_code, publish_status="DEAD")

    async def _mark_failed(
        self,
        message: ClaimedOutboxMessage,
        claim_id: str,
        error_code: str,
        *,
        publish_status: Literal["PENDING", "DEAD"],
    ) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(OutboxMessageRecord)
                .where(
                    OutboxMessageRecord.id == message.id,
                    OutboxMessageRecord.publish_status == "PUBLISHING",
                    OutboxMessageRecord.claimed_by == claim_id,
                )
                .values(
                    publish_status=publish_status,
                    claimed_by=None,
                    claimed_at=None,
                    published_at=None,
                    last_error_code=error_code[:64],
                    available_at=datetime.now(UTC)
                    + timedelta(seconds=min(2 ** max(message.attempt_count - 1, 0), 60)),
                    row_version=OutboxMessageRecord.row_version + 1,
                )
            )
            await session.commit()


__all__ = [
    "ClaimedOutboxMessage",
    "OutboxStartContextMissingError",
    "SqlAlchemyOutboxRepository",
    "TaskStartContext",
]
