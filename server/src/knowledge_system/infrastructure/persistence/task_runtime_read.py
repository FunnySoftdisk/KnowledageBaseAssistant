"""DEV-06 PG 只读仓库：把任务行、运行时权威与既有运行时表投影为 `RuntimeDecisionFactsV1`。

事实来源分三类：

1. **任务行直读**（`workflow.intelligent_task` + `workflow.task_input_snapshot`）：身份/版本/
   状态/游标/截止时间。
2. **运行时权威**（`workflow.task_runtime_state`，缺省 `RuntimeLoopStateV1()`）：代数栅栏、
   工作版本、阻断/就绪引用、无进展计数器与循环级收敛动作（无既有表可承载的字段）。
3. **既有运行时表派生**（`task_attempt` / `plan_item_runtime` / `task_result` /
   `user_input_request`）：活跃执行单元、未知外部效应、活跃 Replan、就绪/可重试项、
   部分结果、注册唤醒源等活跃信号。

`cancellation_terminal_kind` 与 `scheduling_state_valid` 在本切片取契约缺省（分别 `None`/`True`），
留待上层 Temporal Activity 产出真实信号后再投影。本模块只读、不提交事务。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_system.modules.tasking.application.runtime_decision_facts import (
    RuntimeDecisionFactsV1,
)
from knowledge_system.modules.tasking.application.runtime_state import RuntimeLoopStateV1
from knowledge_system.modules.tasking.domain.runtime_decision import (
    RetryablePlanItemV1,
    WakeSourceV1,
)

from .attempt_models import TaskAttemptRecord
from .planning_models import PlanItemRuntimeRecord
from .runtime_state_repository import TaskRuntimeStateRepository
from .task_models import IntelligentTaskRecord, TaskInputSnapshotRecord, UserInputRequestRecord
from .task_result_models import TaskResultRecord

_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
_ACTIVE_ATTEMPT_STATUSES = frozenset({"CREATED", "RUNNING"})
_PARTIAL_ANSWER_STATUSES = frozenset({"PARTIAL", "INSUFFICIENT_EVIDENCE"})

_WakeKind = Literal["PENDING_DEFERRED", "PENDING_USER_INPUT"]


class RuntimeFactsIntegrityError(RuntimeError):
    """已持久化任务行/输入快照违反运行时事实不变式，无法投影。"""


class RuntimeFactsNotRunnableError(RuntimeError):
    """任务尚未进入可决策状态（缺当前输入或活动计划）。"""


def _any_status(attempts: Sequence[TaskAttemptRecord], statuses: set[str]) -> bool:
    return any(attempt.status in statuses for attempt in attempts)


def _any_effect(attempts: Sequence[TaskAttemptRecord], effect_state: str) -> bool:
    return any(attempt.external_effect_state == effect_state for attempt in attempts)


def _any_kind_in(
    attempts: Sequence[TaskAttemptRecord], kind: str, statuses: set[str]
) -> bool:
    return any(
        attempt.attempt_kind == kind and attempt.status in statuses for attempt in attempts
    )


class TaskRuntimeReadRepository:
    """只读端口适配；事实由任务行 + 运行时权威 + 既有运行时表派生，不改写任何表。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_facts(self, task_id: UUID) -> RuntimeDecisionFactsV1 | None:
        task = await self._get_task(task_id)
        if task is None:
            return None
        if task.current_input_id is None or task.active_plan_version is None:
            raise RuntimeFactsNotRunnableError("RUNTIME_FACTS_TASK_NOT_RUNNABLE")

        input_version = await self._load_input_version(task.current_input_id)
        loop_state = await TaskRuntimeStateRepository(self._session).get(task_id)
        if loop_state is None:
            loop_state = RuntimeLoopStateV1()

        attempts = (
            await self._session.scalars(
                select(TaskAttemptRecord).where(TaskAttemptRecord.task_id == task_id)
            )
        ).all()

        terminal_committed = task.status in _TERMINAL_STATUSES
        has_pending_cancellation = task.status == "CANCEL_REQUESTED"
        has_active_execution_unit = _any_status(attempts, set(_ACTIVE_ATTEMPT_STATUSES))
        has_unresolved_unknown_effect = _any_effect(attempts, "OUTCOME_UNKNOWN")
        has_blocking_unknown_effect = _any_effect(attempts, "RECONCILING")
        has_active_replan = _any_kind_in(attempts, "REPLAN", set(_ACTIVE_ATTEMPT_STATUSES))
        contributions_merged = _any_kind_in(attempts, "FINAL_SYNTHESIS", {"COMPLETED"})

        has_ready_items = await self._has_ready_items(task_id, task.active_plan_version)
        has_unaffected_ready_items = has_ready_items and not has_active_replan
        retryable_items = await self._load_retryable_items(task_id)
        has_partial_results = await self._has_answer_status(
            task_id, set(_PARTIAL_ANSWER_STATUSES)
        )
        required_criteria_satisfied = await self._has_answer_status(task_id, {"COMPLETE"})
        wake_sources = await self._load_wake_sources(task_id)

        cancellation_fence_required = (
            loop_state.execution_generation < loop_state.cancellation_generation
        )
        reconciliation_ready = (
            has_blocking_unknown_effect and loop_state.next_reconcile_at is not None
        )
        runtime_close_ready = (
            terminal_committed
            and not has_active_execution_unit
            and not has_unresolved_unknown_effect
            and not has_active_replan
        )
        snapshot_version_stable = not has_active_replan and not has_pending_cancellation

        return RuntimeDecisionFactsV1(
            task_id=task.id,
            task_row_version=task.version,
            event_high_watermark=task.last_event_sequence,
            db_status=task.status,
            input_id=task.current_input_id,
            input_version=input_version,
            plan_version=task.active_plan_version,
            work_version=loop_state.work_version,
            execution_generation=loop_state.execution_generation,
            cancellation_generation=loop_state.cancellation_generation,
            deadline_at=task.deadline_at,
            terminal_committed=terminal_committed,
            runtime_close_ready=runtime_close_ready,
            has_active_execution_unit=has_active_execution_unit,
            has_unresolved_unknown_effect=has_unresolved_unknown_effect,
            has_pending_cancellation=has_pending_cancellation,
            cancellation_fence_required=cancellation_fence_required,
            cancellation_terminal_kind=None,
            has_blocking_unknown_effect=has_blocking_unknown_effect,
            reconciliation_ready=reconciliation_ready,
            blocking_effect_refs=loop_state.blocking_effect_refs,
            next_reconcile_at=loop_state.next_reconcile_at,
            blocking_issue=loop_state.blocking_issue,
            has_active_replan=has_active_replan,
            snapshot_version_stable=snapshot_version_stable,
            has_unaffected_ready_items=has_unaffected_ready_items,
            retryable_items=retryable_items,
            contributions_merged=contributions_merged,
            has_ready_items=has_ready_items,
            ready_set_ref=loop_state.ready_set_ref,
            scheduling_state_valid=True,
            wake_sources=wake_sources,
            replan_required=loop_state.replan_required,
            convergence_action=loop_state.convergence_action,
            no_progress_state=loop_state.no_progress_state,
            has_partial_results=has_partial_results,
            required_criteria_satisfied=required_criteria_satisfied,
        )

    async def _get_task(self, task_id: UUID) -> IntelligentTaskRecord | None:
        statement: Select[tuple[IntelligentTaskRecord]] = select(IntelligentTaskRecord).where(
            IntelligentTaskRecord.id == task_id
        )
        return (await self._session.scalars(statement)).first()

    async def _load_input_version(self, input_id: UUID) -> int:
        statement: Select[tuple[TaskInputSnapshotRecord]] = select(TaskInputSnapshotRecord).where(
            TaskInputSnapshotRecord.input_id == input_id
        )
        record = (await self._session.scalars(statement)).first()
        if record is None:
            raise RuntimeFactsIntegrityError("RUNTIME_FACTS_INPUT_SNAPSHOT_MISSING")
        return record.input_revision

    async def _has_ready_items(self, task_id: UUID, plan_version: int) -> bool:
        statement: Select[tuple[PlanItemRuntimeRecord]] = select(PlanItemRuntimeRecord).where(
            PlanItemRuntimeRecord.task_id == task_id,
            PlanItemRuntimeRecord.plan_version == plan_version,
            PlanItemRuntimeRecord.status == "READY",
        )
        return (await self._session.scalars(statement)).first() is not None

    async def _load_retryable_items(self, task_id: UUID) -> tuple[RetryablePlanItemV1, ...]:
        statement: Select[tuple[PlanItemRuntimeRecord]] = (
            select(PlanItemRuntimeRecord)
            .where(
                PlanItemRuntimeRecord.task_id == task_id,
                PlanItemRuntimeRecord.status == "BLOCKED",
                PlanItemRuntimeRecord.current_iteration.in_((1, 2)),
            )
            .order_by(PlanItemRuntimeRecord.plan_item_id)
        )
        records = (await self._session.scalars(statement)).all()
        return tuple(
            RetryablePlanItemV1(
                plan_item_id=record.plan_item_id,
                next_iteration_no=record.current_iteration + 1,
                retry_reason="BUSINESS_RETRYABLE_FAILURE",
            )
            for record in records
        )

    async def _has_answer_status(self, task_id: UUID, statuses: set[str]) -> bool:
        statement: Select[tuple[TaskResultRecord]] = select(TaskResultRecord).where(
            TaskResultRecord.task_id == task_id,
            TaskResultRecord.answer_status.in_(tuple(statuses)),
        )
        return (await self._session.scalars(statement)).first() is not None

    async def _load_wake_sources(self, task_id: UUID) -> tuple[WakeSourceV1, ...]:
        statement: Select[tuple[UserInputRequestRecord]] = select(UserInputRequestRecord).where(
            UserInputRequestRecord.task_id == task_id,
            UserInputRequestRecord.status == "PENDING",
            UserInputRequestRecord.expires_at.is_not(None),
        )
        records = (await self._session.scalars(statement)).all()
        sources: list[WakeSourceV1] = []
        for record in records:
            expires_at = record.expires_at
            assert expires_at is not None
            kind: _WakeKind = (
                "PENDING_DEFERRED" if record.origin == "DEFERRED_TOOL" else "PENDING_USER_INPUT"
            )
            sources.append(WakeSourceV1(kind=kind, refs=(), next_wakeup_at=expires_at))
        return tuple(sources)


__all__ = [
    "RuntimeFactsIntegrityError",
    "RuntimeFactsNotRunnableError",
    "TaskRuntimeReadRepository",
]
