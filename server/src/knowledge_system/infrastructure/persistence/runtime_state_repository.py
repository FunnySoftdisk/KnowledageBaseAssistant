"""`workflow.task_runtime_state` 的任务级运行时闭环权威适配器。

实现 `RuntimeLoopStateV1` 与 `TaskRuntimeStateRecord` 的互转（显式 kwargs，无 `model_validate`），
以及按 `row_version` CAS 的 upsert。本模块不提交事务，由上层 UoW/服务在合适边界提交。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_system.modules.tasking.application.runtime_convergence import (
    RuntimeConvergenceActionV1,
)
from knowledge_system.modules.tasking.application.runtime_state import RuntimeLoopStateV1
from knowledge_system.modules.tasking.domain.core_types import VersionedRefV1
from knowledge_system.modules.tasking.domain.runtime_decision import (
    RuntimeBlockingIssueKindV1,
    RuntimeBlockingIssueV1,
)
from knowledge_system.modules.tasking.domain.runtime_progress import NoProgressStateV1

from .runtime_state_models import TaskRuntimeStateRecord

_MANAGED_COLUMNS = frozenset({"task_id", "row_version", "created_at", "updated_at"})


class RuntimeStateRecordIntegrityError(RuntimeError):
    """已持久化列违反运行时状态不变式，无法还原为读模型。"""


class RuntimeStateRowVersionConflict(RuntimeError):
    """upsert 的 expected_row_version 与已持久化行版本不一致。"""


def _ref_to_json(ref: VersionedRefV1) -> dict[str, object]:
    return {
        "ref_id": str(ref.ref_id),
        "version": ref.version,
        "integrity_digest": ref.integrity_digest,
    }


def _refs_to_json(refs: tuple[VersionedRefV1, ...]) -> list[object]:
    return [_ref_to_json(ref) for ref in refs]


def _ref_from_json(value: object) -> VersionedRefV1:
    if not isinstance(value, dict):
        raise RuntimeStateRecordIntegrityError("RUNTIME_STATE_REF_NOT_OBJECT")
    return VersionedRefV1(
        ref_id=UUID(str(value["ref_id"])),
        version=int(value["version"]),
        integrity_digest=str(value["integrity_digest"]),
    )


def _refs_from_json(values: object) -> tuple[VersionedRefV1, ...]:
    if not isinstance(values, list):
        raise RuntimeStateRecordIntegrityError("RUNTIME_STATE_REFS_NOT_ARRAY")
    return tuple(_ref_from_json(value) for value in values)


def _blocking_issue_to_json(issue: RuntimeBlockingIssueV1) -> dict[str, object]:
    return {
        "issue_kind": issue.issue_kind.value,
        "issue_fingerprint": issue.issue_fingerprint,
        "refs": _refs_to_json(issue.refs),
    }


def _blocking_issue_from_json(value: object) -> RuntimeBlockingIssueV1:
    if not isinstance(value, dict):
        raise RuntimeStateRecordIntegrityError("RUNTIME_STATE_ISSUE_NOT_OBJECT")
    return RuntimeBlockingIssueV1(
        issue_kind=RuntimeBlockingIssueKindV1(str(value["issue_kind"])),
        issue_fingerprint=str(value["issue_fingerprint"]),
        refs=_refs_from_json(value["refs"]),
    )


def record_from_state(task_id: UUID, state: RuntimeLoopStateV1) -> TaskRuntimeStateRecord:
    """运行时状态 → ORM 行；`row_version`/`created_at`/`updated_at` 交由 DB/会话默认。"""

    np_state = state.no_progress_state
    return TaskRuntimeStateRecord(
        task_id=task_id,
        work_version=state.work_version,
        execution_generation=state.execution_generation,
        cancellation_generation=state.cancellation_generation,
        next_reconcile_at=state.next_reconcile_at,
        blocking_effect_refs_json=_refs_to_json(state.blocking_effect_refs),
        blocking_issue_json=(
            None if state.blocking_issue is None else _blocking_issue_to_json(state.blocking_issue)
        ),
        ready_set_ref_json=(
            None if state.ready_set_ref is None else _ref_to_json(state.ready_set_ref)
        ),
        current_loop_fingerprint=np_state.current_loop_fingerprint,
        same_loop_no_progress=np_state.same_loop_no_progress,
        consecutive_no_progress=np_state.consecutive_no_progress,
        epoch_no_progress=np_state.epoch_no_progress,
        task_no_progress=np_state.task_no_progress,
        episode_replan_used=np_state.episode_replan_used,
        task_replan_count=np_state.task_replan_count,
        episode_seen_loop_fingerprints_json=list(np_state.episode_seen_loop_fingerprints),
        convergence_action=state.convergence_action.value,
        replan_required=state.replan_required,
    )


def state_from_record(record: TaskRuntimeStateRecord) -> RuntimeLoopStateV1:
    """ORM 行 → 运行时状态（显式 kwargs，无 `model_validate`）。"""

    return RuntimeLoopStateV1(
        work_version=record.work_version,
        execution_generation=record.execution_generation,
        cancellation_generation=record.cancellation_generation,
        next_reconcile_at=record.next_reconcile_at,
        blocking_effect_refs=_refs_from_json(record.blocking_effect_refs_json),
        blocking_issue=(
            None
            if record.blocking_issue_json is None
            else _blocking_issue_from_json(record.blocking_issue_json)
        ),
        ready_set_ref=(
            None if record.ready_set_ref_json is None else _ref_from_json(record.ready_set_ref_json)
        ),
        no_progress_state=NoProgressStateV1(
            current_loop_fingerprint=record.current_loop_fingerprint,
            same_loop_no_progress=record.same_loop_no_progress,
            consecutive_no_progress=record.consecutive_no_progress,
            epoch_no_progress=record.epoch_no_progress,
            task_no_progress=record.task_no_progress,
            episode_replan_used=record.episode_replan_used,
            task_replan_count=record.task_replan_count,
            episode_seen_loop_fingerprints=tuple(
                str(fp) for fp in record.episode_seen_loop_fingerprints_json
            ),
        ),
        convergence_action=(
            RuntimeConvergenceActionV1(record.convergence_action)
            if record.convergence_action is not None
            else RuntimeConvergenceActionV1.CONTINUE_LOOP
        ),
        replan_required=record.replan_required,
    )


class TaskRuntimeStateRepository:
    """任务级运行时状态适配器；持有同 Session，不提交、不开启事务。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, task_id: UUID) -> RuntimeLoopStateV1 | None:
        record = await self._get_record(task_id)
        return None if record is None else state_from_record(record)

    async def upsert(
        self,
        task_id: UUID,
        state: RuntimeLoopStateV1,
        *,
        expected_row_version: int | None = None,
    ) -> tuple[RuntimeLoopStateV1, bool]:
        """插入或按 row_version CAS 覆盖；返回 `(当前状态, 是否新建)`。不提交事务。"""

        existing = await self._get_record(task_id)
        if existing is None:
            self._session.add(record_from_state(task_id, state))
            return state, True
        if expected_row_version is not None and existing.row_version != expected_row_version:
            raise RuntimeStateRowVersionConflict(
                f"RUNTIME_STATE_ROW_VERSION_CONFLICT task={task_id} "
                f"expected={expected_row_version} actual={existing.row_version}"
            )
        updated = record_from_state(task_id, state)
        for column in TaskRuntimeStateRecord.__table__.columns:
            name = column.name
            if name in _MANAGED_COLUMNS:
                continue
            setattr(existing, name, getattr(updated, name))
        existing.row_version = existing.row_version + 1
        return state, False

    async def _get_record(self, task_id: UUID) -> TaskRuntimeStateRecord | None:
        statement: Select[tuple[TaskRuntimeStateRecord]] = select(TaskRuntimeStateRecord).where(
            TaskRuntimeStateRecord.task_id == task_id
        )
        return (await self._session.scalars(statement)).first()


__all__ = [
    "RuntimeStateRecordIntegrityError",
    "RuntimeStateRowVersionConflict",
    "TaskRuntimeStateRepository",
    "record_from_state",
    "state_from_record",
]
