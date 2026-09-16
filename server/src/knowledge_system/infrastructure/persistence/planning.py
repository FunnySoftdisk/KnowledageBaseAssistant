"""Goal/Plan激活写集与CAS的显式Unit of Work适配器；本模块永不提交事务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
from .task_models import (
    IntelligentTaskRecord,
    OutboxMessageRecord,
    TaskEventRecord,
    UserInputRequestRecord,
)

type _PlanChildRow = (
    TaskPlanItemDefinitionRecord
    | TaskPlanHypothesisRecord
    | TaskCompiledCriterionRecord
    | PlanItemRuntimeRecord
)


class PlanActivationConflictError(RuntimeError):
    """并发激活或前置Plan指针不匹配。"""


class GoalCompletionConflictError(RuntimeError):
    """Goal提交遇到Task/Input/Attempt版本漂移。"""


@dataclass(frozen=True, slots=True)
class PlanAttemptCompletion:
    """Plan激活事务内写入的Child成功终态。"""

    task_attempt_id: UUID
    expected_row_version: int
    result_kind: str
    result_schema_version: str
    result_digest: str
    ended_at: datetime

    def validate(self) -> None:
        if self.expected_row_version < 1:
            raise PersistenceContractError("PLAN_ATTEMPT_ROW_VERSION_INVALID")
        # 只有已经编译、校验并可激活的成功Plan才能进入本事务。
        if self.result_kind != "SUCCEEDED":
            raise PersistenceContractError("PLAN_ATTEMPT_RESULT_KIND_INVALID")
        if self.result_schema_version != "attempt_completed_v1":
            raise PersistenceContractError("PLAN_ATTEMPT_RESULT_SCHEMA_INVALID")
        if (
            len(self.result_digest) != 64
            or self.result_digest != self.result_digest.lower()
            or any(character not in "0123456789abcdef" for character in self.result_digest)
        ):
            raise PersistenceContractError("PLAN_ATTEMPT_RESULT_DIGEST_INVALID")
        if self.ended_at.tzinfo is None or self.ended_at.utcoffset() is None:
            raise PersistenceContractError("PLAN_ATTEMPT_ENDED_AT_INVALID")


@dataclass(frozen=True, slots=True)
class GoalCompletionWriteSet:
    """Goal Attempt结果、Gate决定、可选澄清请求及通知的原子写集。"""

    goal_understanding: TaskGoalUnderstandingRecord
    expected_task_version: int
    task_attempt: TaskAttemptRecord | None = None
    user_input_request: UserInputRequestRecord | None = None
    events: tuple[TaskEventRecord, ...] = ()
    outbox_messages: tuple[OutboxMessageRecord, ...] = ()

    def validate_relations(self) -> None:
        goal = self.goal_understanding
        if self.expected_task_version < 1:
            raise PersistenceContractError("GOAL_EXPECTED_TASK_VERSION_INVALID")
        if self.task_attempt is not None:
            attempt = self.task_attempt
            if attempt.id != goal.task_attempt_id:
                raise PersistenceContractError("GOAL_ATTEMPT_ID_MISMATCH")
            if attempt.task_id != goal.task_id or attempt.input_id != goal.input_id:
                raise PersistenceContractError("GOAL_ATTEMPT_SCOPE_MISMATCH")
            if attempt.attempt_kind != "GOAL_UNDERSTANDING" or attempt.plan_version is not None:
                raise PersistenceContractError("GOAL_ATTEMPT_KIND_INVALID")
            if attempt.status != "COMPLETED":
                raise PersistenceContractError("GOAL_ATTEMPT_NOT_COMPLETED")
        request = self.user_input_request
        if goal.gate_outcome == "READY_TO_PLAN":
            if request is not None:
                raise PersistenceContractError("GOAL_READY_REQUEST_FORBIDDEN")
        elif goal.gate_outcome == "REQUEST_USER_INPUT":
            if request is None:
                raise PersistenceContractError("GOAL_CLARIFICATION_REQUEST_REQUIRED")
            if (
                request.task_id != goal.task_id
                or request.input_id != goal.input_id
                or request.source_task_attempt_id != goal.task_attempt_id
                or request.goal_understanding_id != goal.understanding_id
            ):
                raise PersistenceContractError("GOAL_REQUEST_SCOPE_MISMATCH")
            if request.origin != "GOAL_CLARIFICATION" or request.status != "PENDING":
                raise PersistenceContractError("GOAL_REQUEST_STATE_INVALID")
        else:
            raise PersistenceContractError("GOAL_GATE_OUTCOME_INVALID")
        event_ids = [event.id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise PersistenceContractError("GOAL_EVENT_ID_DUPLICATE")
        for event in self.events:
            if event.task_id != goal.task_id:
                raise PersistenceContractError("GOAL_EVENT_TASK_MISMATCH")
        if not any(event.event_type == "GOAL_UNDERSTANDING_COMPLETED" for event in self.events):
            raise PersistenceContractError("GOAL_COMPLETION_EVENT_REQUIRED")
        if goal.gate_outcome == "REQUEST_USER_INPUT" and not any(
            event.event_type == "USER_INPUT_REQUESTED" for event in self.events
        ):
            raise PersistenceContractError("GOAL_USER_INPUT_EVENT_REQUIRED")
        allowed_event_ids = set(event_ids)
        outbox_keys: list[tuple[UUID, str]] = []
        for message in self.outbox_messages:
            if message.event_id not in allowed_event_ids:
                raise PersistenceContractError("GOAL_OUTBOX_EVENT_UNKNOWN")
            if message.aggregate_id != goal.task_id:
                raise PersistenceContractError("GOAL_OUTBOX_TASK_MISMATCH")
            outbox_keys.append((message.event_id, message.destination))
        if len(outbox_keys) != len(set(outbox_keys)):
            raise PersistenceContractError("GOAL_OUTBOX_DUPLICATE")
        if not self.outbox_messages:
            raise PersistenceContractError("GOAL_OUTBOX_REQUIRED")


@dataclass(frozen=True, slots=True)
class PlanActivationWriteSet:
    """Plan定义、Attempt成功终态、运行投影、Event与Outbox的原子激活写集。

    ``attempt_completion``携带Child成功结果的稳定终态字段；``task_attempt``是产出本Plan版本的
    PLAN Attempt（被``plan_version.task_attempt_id``引用），后续Temporal接线应在Child启动前落库，
    当前也允许在本写集内补建RUNNING行。``goal_understanding``被Plan引用；不在写集时必须已在先前
    事务落库。事件序号由Repository锁Task后分配，调用方无需预置``sequence``。
    """

    plan_version: TaskPlanVersionRecord
    attempt_completion: PlanAttemptCompletion | None = None
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
        if self.attempt_completion is not None:
            self.attempt_completion.validate()
            if self.attempt_completion.task_attempt_id != self.plan_version.task_attempt_id:
                raise PersistenceContractError("PLAN_ATTEMPT_COMPLETION_ID_MISMATCH")
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

    async def complete_goal_understanding(self, write_set: GoalCompletionWriteSet) -> None:
        """按Task版本栅栏提交Goal；澄清分支同时创建Request并进入等待态。"""

        write_set.validate_relations()
        goal = write_set.goal_understanding
        task = await self.get_task_for_update(goal.task_id)
        if task is None:
            raise TaskNotFoundError("TASK_NOT_FOUND")

        existing = await self._session.scalar(
            select(TaskGoalUnderstandingRecord).where(
                TaskGoalUnderstandingRecord.task_id == goal.task_id,
                TaskGoalUnderstandingRecord.input_id == goal.input_id,
                TaskGoalUnderstandingRecord.task_attempt_id == goal.task_attempt_id,
            )
        )
        if existing is not None:
            if (
                existing.understanding_digest == goal.understanding_digest
                and existing.gate_digest == goal.gate_digest
                and existing.gate_outcome == goal.gate_outcome
            ):
                return
            raise GoalCompletionConflictError("GOAL_COMPLETION_REPLAY_CONFLICT")
        duplicate_id = await self._session.get(TaskGoalUnderstandingRecord, goal.understanding_id)
        if duplicate_id is not None:
            raise GoalCompletionConflictError("GOAL_COMPLETION_REPLAY_CONFLICT")

        if task.status != "PLANNING":
            raise GoalCompletionConflictError("GOAL_TASK_STATUS_CONFLICT")
        if task.current_input_id != goal.input_id:
            raise GoalCompletionConflictError("GOAL_INPUT_CONFLICT")
        if task.active_plan_version is not None:
            raise GoalCompletionConflictError("GOAL_ACTIVE_PLAN_CONFLICT")
        if task.version != write_set.expected_task_version:
            raise GoalCompletionConflictError("GOAL_TASK_VERSION_CONFLICT")

        attempt = write_set.task_attempt
        if attempt is None:
            attempt = await self._session.get(TaskAttemptRecord, goal.task_attempt_id)
        if (
            attempt is None
            or attempt.task_id != goal.task_id
            or attempt.input_id != goal.input_id
            or attempt.attempt_kind != "GOAL_UNDERSTANDING"
            or attempt.plan_version is not None
            or attempt.status != "COMPLETED"
        ):
            raise GoalCompletionConflictError("GOAL_ATTEMPT_CONFLICT")

        request = write_set.user_input_request
        if request is not None:
            if request.requested_at >= request.expires_at or request.expires_at > task.deadline_at:
                raise GoalCompletionConflictError("GOAL_REQUEST_DEADLINE_CONFLICT")
            clarification_count = await self._session.scalar(
                select(func.count())
                .select_from(UserInputRequestRecord)
                .where(
                    UserInputRequestRecord.task_id == goal.task_id,
                    UserInputRequestRecord.origin == "GOAL_CLARIFICATION",
                )
            )
            if (
                clarification_count is None
                or clarification_count >= 2
                or request.clarification_round_no != clarification_count + 1
            ):
                raise GoalCompletionConflictError("GOAL_CLARIFICATION_ROUND_CONFLICT")

        if write_set.task_attempt is not None:
            self._session.add(write_set.task_attempt)
            await self._session.flush((write_set.task_attempt,))
        self._session.add(goal)
        await self._session.flush((goal,))
        if request is not None:
            self._session.add(request)
            await self._session.flush((request,))

        next_sequence = task.last_event_sequence + 1
        for event in write_set.events:
            event.sequence = next_sequence
            next_sequence += 1
        self._session.add_all(write_set.events)
        await self._session.flush(write_set.events)
        self._session.add_all(write_set.outbox_messages)
        await self._session.flush(write_set.outbox_messages)

        target_status = (
            "WAITING_USER_INPUT" if goal.gate_outcome == "REQUEST_USER_INPUT" else "PLANNING"
        )
        statement = (
            update(IntelligentTaskRecord)
            .where(
                IntelligentTaskRecord.id == goal.task_id,
                IntelligentTaskRecord.status == "PLANNING",
                IntelligentTaskRecord.current_input_id == goal.input_id,
                IntelligentTaskRecord.active_plan_version.is_(None),
                IntelligentTaskRecord.version == write_set.expected_task_version,
                IntelligentTaskRecord.deadline_at > func.now(),
            )
            .values(
                status=target_status,
                last_event_sequence=task.last_event_sequence + len(write_set.events),
                version=IntelligentTaskRecord.version + 1,
            )
            .returning(IntelligentTaskRecord.id)
        )
        result = await self._session.execute(statement)
        if result.scalar_one_or_none() is None:
            raise GoalCompletionConflictError("GOAL_COMPLETION_CAS_CONFLICT")

    async def activate_plan_version(self, write_set: PlanActivationWriteSet) -> None:
        """原子激活唯一Plan，并以CAS完成对应PLAN Attempt。"""

        write_set.validate_relations()
        task_id = write_set.plan_version.task_id
        plan_version = write_set.plan_version.plan_version
        predecessor = write_set.plan_version.predecessor_version
        completion = write_set.attempt_completion
        if completion is None:
            raise PersistenceContractError("PLAN_ATTEMPT_COMPLETION_REQUIRED")

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
                existing_attempt = await self._session.get(
                    TaskAttemptRecord, write_set.plan_version.task_attempt_id
                )
                if (
                    existing_attempt is not None
                    and existing_attempt.status == "COMPLETED"
                    and existing_attempt.result_kind == completion.result_kind
                    and existing_attempt.result_schema_version == completion.result_schema_version
                    and existing_attempt.result_digest == completion.result_digest
                    and existing_attempt.ended_at == completion.ended_at
                ):
                    return
                raise PlanActivationConflictError("PLAN_ATTEMPT_TERMINAL_STATE_CONFLICT")
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
            attempt = await self._session.scalar(
                select(TaskAttemptRecord)
                .where(TaskAttemptRecord.id == write_set.plan_version.task_attempt_id)
                .with_for_update()
            )
        if (
            attempt is None
            or attempt.task_id != task_id
            or attempt.input_id != task.current_input_id
            or attempt.attempt_kind != "PLAN"
            or attempt.plan_version != 1
            or attempt.agent_instance_id != write_set.plan_version.created_by_agent_instance_id
            or attempt.status != "RUNNING"
            or attempt.row_version != completion.expected_row_version
        ):
            raise PlanActivationConflictError("PLAN_ACTIVATION_ATTEMPT_CONFLICT")
        if (attempt.created_at is not None and completion.ended_at < attempt.created_at) or (
            attempt.started_at is not None and completion.ended_at < attempt.started_at
        ):
            raise PlanActivationConflictError("PLAN_ATTEMPT_COMPLETION_TIME_CONFLICT")
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

        attempt_statement = (
            update(TaskAttemptRecord)
            .where(
                TaskAttemptRecord.id == completion.task_attempt_id,
                TaskAttemptRecord.task_id == task_id,
                TaskAttemptRecord.input_id == write_set.plan_version.input_id,
                TaskAttemptRecord.attempt_kind == "PLAN",
                TaskAttemptRecord.plan_version == plan_version,
                TaskAttemptRecord.status == "RUNNING",
                TaskAttemptRecord.row_version == completion.expected_row_version,
            )
            .values(
                status="COMPLETED",
                result_kind=completion.result_kind,
                result_schema_version=completion.result_schema_version,
                result_digest=completion.result_digest,
                ended_at=completion.ended_at,
                row_version=TaskAttemptRecord.row_version + 1,
            )
            .returning(TaskAttemptRecord.id)
        )
        attempt_result = await self._session.execute(attempt_statement)
        if attempt_result.scalar_one_or_none() is None:
            raise PlanActivationConflictError("PLAN_ATTEMPT_COMPLETION_CAS_CONFLICT")

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
