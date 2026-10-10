"""`workflow.task_result` 终态结果 SQLAlchemy 适配器。

实现 §5.34 幂等提交语义：插入按 `(task_id, result_version)` 幂等（`INSERT ... ON CONFLICT DO
NOTHING`），冲突时精确区分「同一 Commit Digest → IDEMPOTENT_REPLAY 返回既有结果」与「同版本
不同 Digest → CONFLICT 进入对账、不得覆盖」。首版仅接受 `result_version=1`，其余以稳定契约错误
失败关闭。本模块不提交事务，由上层 UoW/服务在单一 PG 事务边界提交。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_system.modules.tasking.application.final_commit import (
    TaskResultCommitDisposition,
    TaskResultCommitResult,
    TaskResultCommitV1,
)

from .task_result_models import TASK_RESULT_CONTRACT_VERSION, TaskResultRecord


class TaskResultRecordIntegrityError(RuntimeError):
    """已持久化列违反 task_result 不变式，无法还原为读模型。"""


class TaskResultCommitContractError(RuntimeError):
    """提交调用违反终态提交契约（如首版之外的 result_version）。"""


def record_from_commit(commit: TaskResultCommitV1) -> TaskResultRecord:
    """写模型 → ORM 行：Answer 状态取枚举值、Criterion/Constraint 快照 JCS 序列化为 JSONB、
    Degradation Flag 展平为 `text[]`，`contract_version` 固定为 `task_result_v1`。"""

    return TaskResultRecord(
        task_id=commit.task_id,
        contract_version=TASK_RESULT_CONTRACT_VERSION,
        result_version=commit.result_version,
        answer_status=commit.answer_status.value,
        answer_artifact_id=commit.answer_artifact_id,
        answer_sha256=commit.answer_sha256,
        result_envelope_artifact_id=commit.result_envelope_artifact_id,
        citation_count=commit.citation_count,
        result_artifact_count=commit.result_artifact_count,
        criterion_checks=[check.model_dump(mode="json") for check in commit.criterion_checks],
        constraint_checks=[check.model_dump(mode="json") for check in commit.constraint_checks],
        degradation_flags=list(commit.degradation_flags),
        final_snapshot_id=commit.final_snapshot_id,
        final_attempt_id=commit.final_attempt_id,
        candidate_artifact_id=commit.candidate_artifact_id,
        validation_artifact_id=commit.validation_artifact_id,
        criterion_evaluation_set_artifact_id=commit.criterion_evaluation_set_artifact_id,
        completion_gate_decision_artifact_id=commit.completion_gate_decision_artifact_id,
        renderer_profile=commit.renderer_profile,
        renderer_profile_digest=commit.renderer_profile_digest,
        result_commit_digest=commit.result_commit_digest,
        commit_receipt_id=commit.commit_receipt_id,
        generated_at=commit.generated_at,
        row_version=1,
    )


class TaskResultRepository:
    """终态结果适配器；持有同 Session，不提交、不开启事务。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_committed(self, commit: TaskResultCommitV1) -> TaskResultCommitResult:
        if commit.result_version != 1:
            raise TaskResultCommitContractError("TASK_RESULT_FIRST_VERSION_ONLY")
        record = record_from_commit(commit)
        values = {
            column.name: getattr(record, column.name)
            for column in TaskResultRecord.__table__.columns
        }
        statement = (
            pg_insert(TaskResultRecord)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["task_id", "result_version"])
            .returning(TaskResultRecord.task_id, TaskResultRecord.result_version)
        )
        inserted = (await self._session.execute(statement)).first()
        if inserted is not None:
            return TaskResultCommitResult(
                disposition=TaskResultCommitDisposition.INSERTED,
                task_id=commit.task_id,
                result_version=commit.result_version,
                result_commit_digest=commit.result_commit_digest,
                row_version=1,
            )
        existing = await self._get_by_task_version(commit.task_id, commit.result_version)
        if existing is None:
            raise TaskResultRecordIntegrityError("TASK_RESULT_INSERT_CONFLICT_MISSING_ROW")
        disposition = (
            TaskResultCommitDisposition.IDEMPOTENT_REPLAY
            if existing.result_commit_digest == commit.result_commit_digest
            else TaskResultCommitDisposition.CONFLICT
        )
        return TaskResultCommitResult(
            disposition=disposition,
            task_id=commit.task_id,
            result_version=commit.result_version,
            result_commit_digest=existing.result_commit_digest,
            row_version=existing.row_version,
        )

    async def get_commit_digest(self, task_id: UUID) -> str | None:
        """按 Task 取既有 `result_commit_digest`，用于同版本不同 Digest 的对账读取。"""

        statement: Select[tuple[str]] = select(TaskResultRecord.result_commit_digest).where(
            TaskResultRecord.task_id == task_id
        )
        return (await self._session.scalars(statement)).first()

    async def _get_by_task_version(
        self, task_id: UUID, result_version: int
    ) -> TaskResultRecord | None:
        statement: Select[tuple[TaskResultRecord]] = select(TaskResultRecord).where(
            TaskResultRecord.task_id == task_id,
            TaskResultRecord.result_version == result_version,
        )
        return (await self._session.scalars(statement)).first()


__all__ = [
    "TaskResultCommitContractError",
    "TaskResultRecordIntegrityError",
    "TaskResultRepository",
    "record_from_commit",
]
