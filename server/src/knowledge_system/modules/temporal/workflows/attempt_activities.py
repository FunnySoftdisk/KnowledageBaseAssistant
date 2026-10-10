"""DEV-03C：Attempt控制面Activity——认领调度与`task_attempt_id+lease_generation+
row_version+RUNNING`四重CAS终态提交。

认知Activity（`execute_attempt_v1`，MODEL队列）在本批以显式测试替身替换，本模块只冻结其
名称，不实现；认领与终态提交运行在Control队列、Workflow沙箱之外，可自由访问SQLAlchemy。
四重CAS是旧Generation与迟到结果隔离的唯一边界：任一令牌不匹配即拒绝提交。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from knowledge_system.infrastructure.persistence.attempt_models import TaskAttemptRecord
from knowledge_system.modules.temporal.domain.contracts import (
    AttemptCommitOutcomeV1,
    AttemptDispatchV1,
    AttemptKindV1,
    AttemptTerminalCommitV1,
)

# 认知Activity名称（MODEL队列）；本批无真实实现，测试用替身替换。
EXECUTE_ATTEMPT_ACTIVITY = "execute_attempt_v1"
COMMIT_ATTEMPT_TERMINAL_STATE_ACTIVITY = "commit_attempt_terminal_state_v1"
CLAIM_NEXT_ATTEMPT_ACTIVITY = "claim_next_attempt_v1"


class AttemptActivities:
    """Attempt认领与终态CAS Activity；依赖以构造注入，不持有全局Session。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @activity.defn(name=CLAIM_NEXT_ATTEMPT_ACTIVITY)
    async def claim_next_attempt(self, task_id: UUID) -> AttemptDispatchV1 | None:
        """认领最早的`CREATED` Attempt并置`RUNNING`，递增`lease_generation`与`row_version`。

        递增后的二者即本Generation的fencing令牌，随`AttemptDispatchV1`返回给Root；
        旧Generation的Child因持有旧令牌而无法提交。
        """

        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    select(TaskAttemptRecord)
                    .where(
                        TaskAttemptRecord.task_id == task_id,
                        TaskAttemptRecord.status == "CREATED",
                    )
                    .order_by(TaskAttemptRecord.attempt_no, TaskAttemptRecord.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).first()
            if row is None:
                await session.commit()
                return None

            attempt_id = row.id
            attempt_kind = row.attempt_kind
            plan_version = row.plan_version
            row.status = "RUNNING"
            row.started_at = datetime.now(UTC)
            row.lease_generation = row.lease_generation + 1
            row.row_version = row.row_version + 1
            lease_generation = row.lease_generation
            attempt_row_version = row.row_version
            await session.commit()

        return AttemptDispatchV1(
            task_attempt_id=attempt_id,
            attempt_kind=AttemptKindV1(attempt_kind),
            plan_version=plan_version,
            lease_generation=lease_generation,
            attempt_row_version=attempt_row_version,
        )

    @activity.defn(name=COMMIT_ATTEMPT_TERMINAL_STATE_ACTIVITY)
    async def commit_attempt_terminal_state(
        self, commit: AttemptTerminalCommitV1
    ) -> AttemptCommitOutcomeV1:
        """四重CAS终态提交；冲突时精确区分旧Generation/行版本/非RUNNING/不存在。"""

        async with self._session_factory() as session:
            statement = (
                update(TaskAttemptRecord)
                .where(
                    TaskAttemptRecord.id == commit.task_attempt_id,
                    TaskAttemptRecord.task_id == commit.task_id,
                    TaskAttemptRecord.lease_generation == commit.lease_generation,
                    TaskAttemptRecord.row_version == commit.expected_row_version,
                    TaskAttemptRecord.status == "RUNNING",
                )
                .values(
                    status=commit.terminal_state,
                    result_kind=commit.result_kind,
                    result_schema_version=commit.result_schema_version,
                    result_digest=commit.result_digest,
                    suspension_kind=commit.suspension_kind,
                    error_code=commit.error_code,
                    failure_class=commit.failure_class,
                    ended_at=datetime.now(UTC),
                    row_version=TaskAttemptRecord.row_version + 1,
                )
                .returning(TaskAttemptRecord.id)
            )
            committed_id = (await session.execute(statement)).scalar_one_or_none()
            if committed_id is not None:
                await session.commit()
                return AttemptCommitOutcomeV1(
                    task_attempt_id=commit.task_attempt_id, committed=True
                )

            row = (
                await session.scalars(
                    select(TaskAttemptRecord).where(
                        TaskAttemptRecord.id == commit.task_attempt_id
                    )
                )
            ).first()
            row_task_id = row.task_id if row is not None else None
            row_lease_generation = row.lease_generation if row is not None else None
            row_row_version = row.row_version if row is not None else None
            await session.rollback()
            conflict_reason: Literal[
                "STALE_LEASE_GENERATION", "ROW_VERSION_CONFLICT", "NOT_RUNNING", "NOT_FOUND"
            ]
            if row_task_id is None or row_task_id != commit.task_id:
                conflict_reason = "NOT_FOUND"
            elif row_lease_generation != commit.lease_generation:
                conflict_reason = "STALE_LEASE_GENERATION"
            elif row_row_version != commit.expected_row_version:
                conflict_reason = "ROW_VERSION_CONFLICT"
            else:
                conflict_reason = "NOT_RUNNING"
            return AttemptCommitOutcomeV1(
                task_attempt_id=commit.task_attempt_id,
                committed=False,
                conflict_reason=conflict_reason,
            )


__all__ = [
    "AttemptActivities",
    "CLAIM_NEXT_ATTEMPT_ACTIVITY",
    "COMMIT_ATTEMPT_TERMINAL_STATE_ACTIVITY",
    "EXECUTE_ATTEMPT_ACTIVITY",
]
