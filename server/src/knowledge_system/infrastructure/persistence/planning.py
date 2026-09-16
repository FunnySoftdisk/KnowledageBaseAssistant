"""Goal/Plan激活写集与CAS的显式Unit of Work适配器；本模块永不提交事务。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .attempt_models import TaskAttemptRecord
from .planning_models import (
    PlanItemRuntimeRecord,
    TaskCompiledCriterionRecord,
    TaskGoalUnderstandingRecord,
    TaskPlanHypothesisRecord,
    TaskPlanItemDefinitionRecord,
    TaskPlanVersionRecord,
)
from .repositories import PersistenceContractError, TaskNotFoundError
from .task_models import IntelligentTaskRecord, OutboxMessageRecord, TaskEventRecord

type _PlanChildRow = (
    TaskPlanItemDefinitionRecord
    | TaskPlanHypothesisRecord
    | TaskCompiledCriterionRecord
    | PlanItemRuntimeRecord
)


class PlanActivationConflictError(RuntimeError):
    """并发激活或前置Plan指针不匹配。"""


@dataclass(frozen=True, slots=True)
class PlanActivationWriteSet:
    """单个Plan版本及其不可变定义/运行投影、Goal、Event与Outbox的原子激活写集。

    ``task_attempt``是产出本Plan版本的PLAN/REPLAN Attempt（被``plan_version.task_attempt_id``
    引用）；``goal_understanding``被``plan_version.goal_understanding_id``引用。二者的Goal Attempt
    若不在本写集内，必须已在先前事务落库（由数据库外键复核）。事件序号由Repository锁Task后
    分配，调用方无需预置``sequence``。
    """

    plan_version: TaskPlanVersionRecord
    task_attempt: TaskAttemptRecord | None = None
    goal_understanding: TaskGoalUnderstandingRecord | None = None
    items: tuple[TaskPlanItemDefinitionRecord, ...] = ()
    hypotheses: tuple[TaskPlanHypothesisRecord, ...] = ()
    criteria: tuple[TaskCompiledCriterionRecord, ...] = ()
    runtime_rows: tuple[PlanItemRuntimeRecord, ...] = ()
    events: tuple[TaskEventRecord, ...] = ()
    outbox_messages: tuple[OutboxMessageRecord, ...] = ()

    def validate_relations(self) -> None:
        task_id = self.plan_version.task_id
        plan_version = self.plan_version.plan_version
        if self.plan_version.status != "ACTIVE":
            raise PersistenceContractError("PLAN_ACTIVATION_STATUS_NOT_ACTIVE")
        if self.plan_version.activated_at is None or self.plan_version.completed_at is not None:
            raise PersistenceContractError("PLAN_ACTIVATION_TIME_SHAPE_INVALID")
        predecessor = self.plan_version.predecessor_version
        if predecessor is not None and predecessor >= plan_version:
            raise PersistenceContractError("PLAN_PREDECESSOR_INVALID")
        children: tuple[_PlanChildRow, ...] = (
            *self.items,
            *self.hypotheses,
            *self.criteria,
            *self.runtime_rows,
        )
        for row in children:
            if row.task_id != task_id or row.plan_version != plan_version:
                raise PersistenceContractError("PLAN_CHILD_TASK_OR_VERSION_MISMATCH")
        item_ids = [item.plan_item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise PersistenceContractError("PLAN_ITEM_ID_DUPLICATE")
        runtime_item_ids = [runtime.plan_item_id for runtime in self.runtime_rows]
        if set(item_ids) != set(runtime_item_ids):
            raise PersistenceContractError("PLAN_RUNTIME_ITEM_COVERAGE_MISMATCH")
        if len(runtime_item_ids) != len(set(runtime_item_ids)):
            raise PersistenceContractError("PLAN_RUNTIME_ITEM_DUPLICATE")
        if self.task_attempt is not None:
            if self.task_attempt.id != self.plan_version.task_attempt_id:
                raise PersistenceContractError("PLAN_ATTEMPT_ID_MISMATCH")
            if self.task_attempt.task_id != task_id:
                raise PersistenceContractError("PLAN_ATTEMPT_TASK_MISMATCH")
            if self.task_attempt.input_id != self.plan_version.input_id:
                raise PersistenceContractError("PLAN_ATTEMPT_INPUT_MISMATCH")
            if (
                self.task_attempt.agent_instance_id
                != self.plan_version.created_by_agent_instance_id
            ):
                raise PersistenceContractError("PLAN_ATTEMPT_AGENT_MISMATCH")
        if self.goal_understanding is not None:
            if self.goal_understanding.understanding_id != self.plan_version.goal_understanding_id:
                raise PersistenceContractError("PLAN_GOAL_ID_MISMATCH")
            if self.goal_understanding.task_id != task_id:
                raise PersistenceContractError("PLAN_GOAL_TASK_MISMATCH")
            if self.goal_understanding.input_id != self.plan_version.input_id:
                raise PersistenceContractError("PLAN_GOAL_INPUT_MISMATCH")
        event_ids = [event.id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise PersistenceContractError("PLAN_EVENT_ID_DUPLICATE")
        for event in self.events:
            if event.task_id != task_id:
                raise PersistenceContractError("PLAN_EVENT_TASK_MISMATCH")
        allowed_event_ids = set(event_ids)
        outbox_keys: list[tuple[UUID, str]] = []
        for message in self.outbox_messages:
            if message.event_id not in allowed_event_ids:
                raise PersistenceContractError("PLAN_OUTBOX_EVENT_UNKNOWN")
            outbox_keys.append((message.event_id, message.destination))
        if len(outbox_keys) != len(set(outbox_keys)):
            raise PersistenceContractError("PLAN_OUTBOX_DUPLICATE")


class PlanTransactionRepository:
    """Goal/Plan定义与激活的SQLAlchemy适配器。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def activate_plan_version(self, write_set: PlanActivationWriteSet) -> None:
        """锁Task后按FK顺序原子写Attempt/Goal/Plan/定义/运行/Event/Outbox，并以CAS切换唯一Active指针。"""

        write_set.validate_relations()
        task_id = write_set.plan_version.task_id
        plan_version = write_set.plan_version.plan_version
        predecessor = write_set.plan_version.predecessor_version

        task = await self.get_task_for_update(task_id)
        if task is None:
            raise TaskNotFoundError("TASK_NOT_FOUND")
        if task.active_plan_version != predecessor:
            # 已提交的同Attempt/同Compiled Digest重放不再写Event或Outbox。
            existing = await self._session.get(TaskPlanVersionRecord, (task_id, plan_version))
            if (
                task.active_plan_version == plan_version
                and existing is not None
                and existing.task_attempt_id == write_set.plan_version.task_attempt_id
                and existing.compiled_plan_digest == write_set.plan_version.compiled_plan_digest
                and existing.input_id == write_set.plan_version.input_id
                and existing.goal_understanding_id == write_set.plan_version.goal_understanding_id
                and existing.status == "ACTIVE"
            ):
                return
            raise PlanActivationConflictError("PLAN_ACTIVATION_PREDECESSOR_CONFLICT")
        # DEV-01只承诺首轮激活；REPLAN还需要执行栅栏、旧Plan处置和Carry事务。
        if predecessor is not None or plan_version != 1:
            raise PersistenceContractError("PLAN_REPLAN_ACTIVATION_NOT_IMPLEMENTED")
        if write_set.plan_version.revision_reason != "NEW_TASK":
            raise PersistenceContractError("PLAN_INITIAL_REVISION_REASON_INVALID")
        if task.status != "PLANNING":
            raise PlanActivationConflictError("PLAN_ACTIVATION_TASK_STATUS_CONFLICT")
        if task.current_input_id != write_set.plan_version.input_id:
            raise PlanActivationConflictError("PLAN_ACTIVATION_INPUT_CONFLICT")
        goal = write_set.goal_understanding
        if goal is None:
            goal = await self._session.get(
                TaskGoalUnderstandingRecord, write_set.plan_version.goal_understanding_id
            )
        if (
            goal is None
            or goal.task_id != task_id
            or goal.input_id != task.current_input_id
            or goal.gate_outcome != "READY_TO_PLAN"
        ):
            raise PlanActivationConflictError("PLAN_ACTIVATION_GOAL_CONFLICT")
        goal_attempt = await self._session.get(TaskAttemptRecord, goal.task_attempt_id)
        if (
            goal_attempt is None
            or goal_attempt.task_id != task_id
            or goal_attempt.input_id != task.current_input_id
            or goal_attempt.attempt_kind != "GOAL_UNDERSTANDING"
            or goal_attempt.status != "COMPLETED"
        ):
            raise PlanActivationConflictError("PLAN_ACTIVATION_GOAL_ATTEMPT_CONFLICT")
        attempt = write_set.task_attempt
        if attempt is None:
            attempt = await self._session.get(
                TaskAttemptRecord, write_set.plan_version.task_attempt_id
            )
        if (
            attempt is None
            or attempt.task_id != task_id
            or attempt.input_id != task.current_input_id
            or attempt.attempt_kind != "PLAN"
            or attempt.plan_version != 1
            or attempt.agent_instance_id != write_set.plan_version.created_by_agent_instance_id
            or attempt.status != "RUNNING"
        ):
            raise PlanActivationConflictError("PLAN_ACTIVATION_ATTEMPT_CONFLICT")
        if not any(event.event_type == "PLAN_ACTIVATED" for event in write_set.events):
            raise PersistenceContractError("PLAN_ACTIVATION_EVENT_REQUIRED")
        if not write_set.outbox_messages:
            raise PersistenceContractError("PLAN_ACTIVATION_OUTBOX_REQUIRED")

        # FK顺序：Attempt → Goal → Plan版本 → 定义子表 → 运行行 → Event → Outbox。
        if write_set.task_attempt is not None:
            self._session.add(write_set.task_attempt)
            await self._session.flush((write_set.task_attempt,))
        if write_set.goal_understanding is not None:
            self._session.add(write_set.goal_understanding)
            await self._session.flush((write_set.goal_understanding,))
        self._session.add(write_set.plan_version)
        await self._session.flush((write_set.plan_version,))
        definitions = (*write_set.items, *write_set.hypotheses, *write_set.criteria)
        self._session.add_all(definitions)
        await self._session.flush(definitions)
        self._session.add_all(write_set.runtime_rows)

        # 事件序号只能通过锁Task并递增last_event_sequence取得。
        next_sequence = task.last_event_sequence + 1
        for event in write_set.events:
            event.sequence = next_sequence
            next_sequence += 1
        if write_set.events:
            self._session.add_all(write_set.events)
            await self._session.flush(write_set.events)
        if write_set.outbox_messages:
            self._session.add_all(write_set.outbox_messages)
            await self._session.flush(write_set.outbox_messages)

        last_event_sequence = task.last_event_sequence + len(write_set.events)
        activated_id = await self.cas_activate_task(
            task_id, predecessor, plan_version, write_set.plan_version.input_id, last_event_sequence
        )
        if activated_id is None:
            raise PlanActivationConflictError("PLAN_ACTIVATION_CAS_CONFLICT")

    async def get_task_for_update(self, task_id: UUID) -> IntelligentTaskRecord | None:
        statement: Select[tuple[IntelligentTaskRecord]] = select(IntelligentTaskRecord).where(
            IntelligentTaskRecord.id == task_id
        )
        result = await self._session.scalars(statement.with_for_update())
        return result.first()

    async def cas_activate_task(
        self,
        task_id: UUID,
        predecessor_version: int | None,
        plan_version: int,
        input_id: UUID,
        last_event_sequence: int,
    ) -> UUID | None:
        statement = (
            update(IntelligentTaskRecord)
            .where(
                IntelligentTaskRecord.id == task_id,
                IntelligentTaskRecord.active_plan_version == predecessor_version,
                IntelligentTaskRecord.status == "PLANNING",
                IntelligentTaskRecord.current_input_id == input_id,
                IntelligentTaskRecord.deadline_at > func.now(),
            )
            .values(
                active_plan_version=plan_version,
                status="RUNNING",
                last_event_sequence=last_event_sequence,
            )
            .returning(IntelligentTaskRecord.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()
