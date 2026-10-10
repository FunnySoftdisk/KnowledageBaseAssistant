"""DEV-03E：Root `AgentTaskWorkflow`可靠执行骨架。

只承载确定性调度、Signal、Query、截止时间、Continue-As-New与取消；LLM/检索/数据库/Tool
一律进Activity。首Run处理START；续跑处理CONTINUE，恢复运行态/预算后进入同一主循环。

主循环：业务事件游标推进 → Attempt认领 → 派发`AgentAttemptWorkflow` Child（确定性ID、
`ParentClosePolicy.TERMINATE`、`REJECT_DUPLICATE`）→ 等待Child终态 → 回环；单Run派发
`MAX_ATTEMPTS_PER_ROOT_RUN`次后Continue-As-New约束History。取消双路径收敛：
业务取消以`TASK_CANCELLED`事件经事件游标识别并终态CAS；Temporal级取消以`CancelledError`
捕获后同样落PG终态再重抛，Child依TERMINATE策略终止（无孤儿Attempt）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, NoReturn, cast
from uuid import UUID

from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, is_cancelled_exception

from knowledge_system.modules.temporal.domain.activity_profiles import (
    ActivityProfileKindV1,
)
from knowledge_system.modules.temporal.domain.contracts import (
    AgentAttemptWorkflowInputV1,
    AgentRegistrationKeyV1,
    AgentTaskRootStateV1,
    AgentTaskRunStateV1,
    AgentTaskRuntimeStateV1,
    AgentTaskWorkflowArgsV1,
    AgentTaskWorkflowContinueV1,
    AgentTaskWorkflowResultV1,
    AgentTaskWorkflowStartV1,
    ApplyBusinessEventsCommandV1,
    ApplyBusinessEventsResultV1,
    AttemptDispatchV1,
    AttemptKindV1,
    RootTerminalCommitV1,
    TaskBudgetV1,
    TaskRefV1,
)
from knowledge_system.modules.temporal.domain.topology import (
    CONTROL_TASK_QUEUE,
    MAX_ATTEMPTS_PER_ROOT_RUN,
    WORKFLOW_TASK_QUEUE,
    WORKFLOW_TASK_TIMEOUT_SECONDS,
)
from knowledge_system.modules.temporal.domain.workflow_ids import (
    BUSINESS_EVENT_SIGNAL,
    RUNTIME_STATE_QUERY,
    agent_attempt_workflow_id,
)
from knowledge_system.modules.temporal.workflows.agent_attempt import AgentAttemptWorkflow
from knowledge_system.modules.temporal.workflows.attempt_activities import (
    CLAIM_NEXT_ATTEMPT_ACTIVITY,
)
from knowledge_system.modules.temporal.workflows.control_activities import (
    APPLY_BUSINESS_EVENTS_ACTIVITY,
    COMMIT_ROOT_TERMINAL_STATE_ACTIVITY,
)
from knowledge_system.modules.temporal.workflows.profiles import activity_execute_kwargs

_TASK_DEADLINE_EXCEEDED = "TASK_DEADLINE_EXCEEDED"
_TASK_CANCELLED_BY_TEMPORAL = "TASK_CANCELLED_BY_TEMPORAL"


@workflow.defn(name="AgentTaskWorkflow")
class AgentTaskWorkflow:
    """Root Workflow：任务生命周期、事件游标、截止时间与取消。"""

    def __init__(self) -> None:
        self._task_id: UUID | None = None
        self._run_sequence = 1
        self._run_state = AgentTaskRunStateV1.STARTED
        self._event_cursor = 0
        self._business_cursor = 0
        self._deadline: datetime | None = None
        self._budget: TaskBudgetV1 | None = None
        self._attempt_kind: AttemptKindV1 | None = None
        self._agent_key = AgentRegistrationKeyV1.SUPERVISOR
        self._needs_reconcile = False
        self._events_pending = False
        self._attempts_dispatched = 0

    @workflow.run
    async def run(self, args: AgentTaskWorkflowArgsV1) -> AgentTaskWorkflowResultV1:
        command = args.root
        if isinstance(command, AgentTaskWorkflowStartV1):
            self._task_id = command.task.task_id
            self._run_sequence = command.run_sequence
            self._budget = command.budget
            self._deadline = command.budget.wall_clock_deadline
            self._event_cursor = command.task_created_event.sequence
            self._business_cursor = command.task_created_event.sequence
            self._run_state = AgentTaskRunStateV1.RUNNING
            return await self._run_loop()
        if isinstance(command, AgentTaskWorkflowContinueV1):
            return await self._run_continue(command)
        raise ApplicationError(
            "UNKNOWN_WORKFLOW_COMMAND", type="UNKNOWN_WORKFLOW_COMMAND", non_retryable=True
        )

    async def _run_continue(
        self, continue_: AgentTaskWorkflowContinueV1
    ) -> AgentTaskWorkflowResultV1:
        """续跑：从Continue输入恢复运行态与预算，再进入同一主循环。"""

        self._task_id = continue_.task.task_id
        self._run_sequence = continue_.run_sequence
        self._budget = continue_.budget
        self._deadline = continue_.budget.wall_clock_deadline
        self._event_cursor = continue_.root_state.event_cursor
        self._business_cursor = continue_.root_state.business_cursor
        self._run_state = continue_.root_state.run_state
        self._attempt_kind = continue_.root_state.attempt_kind
        self._agent_key = (
            continue_.root_state.agent_registration_key or AgentRegistrationKeyV1.SUPERVISOR
        )
        self._needs_reconcile = continue_.root_state.needs_reconcile
        return await self._run_loop()

    async def _run_loop(self) -> AgentTaskWorkflowResultV1:
        try:
            while True:
                if self._events_pending:
                    self._events_pending = False
                    applied = await self._apply_business_events()
                    self._event_cursor = applied.new_cursor
                    self._business_cursor = applied.new_cursor
                    if applied.terminal_requested is not None:
                        return await self._commit_terminal(
                            AgentTaskRunStateV1(applied.terminal_requested),
                            f"TASK_{applied.terminal_requested}",
                        )
                    continue

                # Continue-As-New边界：单Run派发够多次Attempt且无未决事件时续跑约束
                # History长度；放在事件处理之后，避免信号尚未推进游标就续跑丢失事件。
                if self._attempts_dispatched >= MAX_ATTEMPTS_PER_ROOT_RUN:
                    self._continue_as_new()  # NoReturn：抛ContinueAsNewError，不返回

                now = workflow.now()
                if now >= self._require_deadline():
                    return await self._commit_terminal(
                        AgentTaskRunStateV1.FAILED, _TASK_DEADLINE_EXCEEDED
                    )

                # Attempt调度：认领下一个CREATED Attempt；有则派发Child并等待其终态。
                dispatch = await self._claim_next_attempt()
                if dispatch is not None:
                    await self._run_attempt(dispatch)
                    self._attempts_dispatched += 1
                    continue

                remaining = (self._require_deadline() - now).total_seconds()
                # Signal或截止时间唤醒；`wait_condition`带timeout时超时即抛`TimeoutError`，
                # 需捕获后回循环顶重新检查截止时间，否则异常会漏出`run`使Workflow失败。
                try:
                    await workflow.wait_condition(
                        lambda: self._events_pending, timeout=remaining
                    )
                except TimeoutError:
                    continue
        except (asyncio.CancelledError, Exception) as err:
            # PG优先对账：Temporal级取消也把Root终态落PG，避免遗留RUNNING；随后重抛让
            # Temporal标记Root CANCELLED，并依TERMINATE策略终止Child（无孤儿Attempt）。
            # 取消不以单一异常形式抵达：直接await中断是`asyncio.CancelledError`，而Child
            # 依`WAIT_CANCELLATION_COMPLETED`取消完成后经`execute_child_workflow`抛
            # `CancelledError`，Activity/Child取消还会以`ActivityError`/`ChildWorkflowError`
            # 携带`CancelledError`根因。统一以`is_cancelled_exception`判别；非取消异常照常
            # 重抛，保持失败语义不变。
            if is_cancelled_exception(err):
                await self._commit_terminal(
                    AgentTaskRunStateV1.CANCELLED, _TASK_CANCELLED_BY_TEMPORAL
                )
            raise

    def _continue_as_new(self) -> NoReturn:
        """确定性Continue-As-New：约束单Run History，携带运行态/预算、序号+1续跑。"""

        workflow.continue_as_new(
            AgentTaskWorkflowArgsV1(
                AgentTaskWorkflowContinueV1(
                    task=TaskRefV1(task_id=self._require_task_id()),
                    root_state=AgentTaskRootStateV1(
                        event_cursor=self._event_cursor,
                        business_cursor=self._business_cursor,
                        run_state=self._run_state,
                        attempt_kind=self._attempt_kind,
                        agent_registration_key=self._agent_key,
                        needs_reconcile=self._needs_reconcile,
                    ),
                    budget=self._require_budget(),
                    run_sequence=self._run_sequence + 1,
                )
            )
        )

    async def _claim_next_attempt(self) -> AttemptDispatchV1 | None:
        task_id = self._require_task_id()
        return cast(
            AttemptDispatchV1 | None,
            await workflow.execute_activity(
                CLAIM_NEXT_ATTEMPT_ACTIVITY,
                task_id,
                result_type=AttemptDispatchV1,
                **_control_execute_kwargs(),
            ),
        )

    async def _run_attempt(self, dispatch: AttemptDispatchV1) -> None:
        """以确定性Child ID派发`AgentAttemptWorkflow`：TERMINATE防孤儿、REJECT_DUPLICATE防重入。"""

        task_id = self._require_task_id()
        self._attempt_kind = dispatch.attempt_kind
        await workflow.execute_child_workflow(
            AgentAttemptWorkflow.run,
            AgentAttemptWorkflowInputV1(
                task_id=task_id,
                task_attempt_id=dispatch.task_attempt_id,
                attempt_kind=dispatch.attempt_kind,
                plan_version=dispatch.plan_version,
                lease_generation=dispatch.lease_generation,
                attempt_row_version=dispatch.attempt_row_version,
                run_sequence=1,
                parent_run_sequence=self._run_sequence,
            ),
            id=agent_attempt_workflow_id(task_id, dispatch.task_attempt_id),
            task_queue=WORKFLOW_TASK_QUEUE,
            parent_close_policy=workflow.ParentClosePolicy.TERMINATE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            cancellation_type=workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
            task_timeout=timedelta(seconds=WORKFLOW_TASK_TIMEOUT_SECONDS),
        )

    async def _apply_business_events(self) -> ApplyBusinessEventsResultV1:
        task_id = self._require_task_id()
        # `execute_activity`按字符串名调用时静态返回`Any`，`result_type`仅在运行时生效，
        # 故以`cast`收窄类型；缺`result_type`时运行时返回dict导致游标AttributeError。
        return cast(
            ApplyBusinessEventsResultV1,
            await workflow.execute_activity(
                APPLY_BUSINESS_EVENTS_ACTIVITY,
                ApplyBusinessEventsCommandV1(
                    task_id=task_id, from_sequence=self._event_cursor
                ),
                result_type=ApplyBusinessEventsResultV1,
                **_control_execute_kwargs(),
            ),
        )

    async def _commit_terminal(
        self, terminal_state: AgentTaskRunStateV1, error_code: str | None
    ) -> AgentTaskWorkflowResultV1:
        task_id = self._require_task_id()
        await workflow.execute_activity(
            COMMIT_ROOT_TERMINAL_STATE_ACTIVITY,
            RootTerminalCommitV1(
                task_id=task_id,
                run_sequence=self._run_sequence,
                terminal_state=terminal_state,
                error_code=error_code,
            ),
            **_control_execute_kwargs(),
        )
        self._run_state = terminal_state
        return AgentTaskWorkflowResultV1(
            task_id=task_id,
            run_sequence=self._run_sequence,
            terminal_state=terminal_state,
            error_code=error_code,
        )

    def _require_task_id(self) -> UUID:
        if self._task_id is None or self._deadline is None:
            raise ApplicationError(
                "WORKFLOW_STATE_NOT_INITIALIZED",
                type="WORKFLOW_STATE_NOT_INITIALIZED",
                non_retryable=True,
            )
        return self._task_id

    def _require_deadline(self) -> datetime:
        deadline = self._deadline
        if deadline is None:
            raise ApplicationError(
                "WORKFLOW_STATE_NOT_INITIALIZED",
                type="WORKFLOW_STATE_NOT_INITIALIZED",
                non_retryable=True,
            )
        return deadline

    def _require_budget(self) -> TaskBudgetV1:
        budget = self._budget
        if budget is None:
            raise ApplicationError(
                "WORKFLOW_STATE_NOT_INITIALIZED",
                type="WORKFLOW_STATE_NOT_INITIALIZED",
                non_retryable=True,
            )
        return budget

    @workflow.signal(name=BUSINESS_EVENT_SIGNAL)
    def business_event_available(self) -> None:
        """通知游标推进：新业务事件已提交，主循环经control Activity按序读取。"""

        self._events_pending = True

    @workflow.query(name=RUNTIME_STATE_QUERY)
    def runtime_state(self) -> AgentTaskRuntimeStateV1:
        task_id = self._require_task_id()
        deadline = self._deadline
        assert deadline is not None  # _require_task_id已保证初始化
        return AgentTaskRuntimeStateV1(
            task_id=task_id,
            run_sequence=self._run_sequence,
            run_state=self._run_state,
            event_cursor=self._event_cursor,
            business_cursor=self._business_cursor,
            attempt_kind=self._attempt_kind,
            agent_registration_key=self._agent_key,
            deadline=deadline,
            needs_reconcile=self._needs_reconcile,
        )


def _control_execute_kwargs() -> dict[str, Any]:
    """CONTROL_TRANSACTION Profile映射为`execute_activity`关键字参数。"""

    return activity_execute_kwargs(ActivityProfileKindV1.CONTROL_TRANSACTION, CONTROL_TASK_QUEUE)


__all__ = ["AgentTaskWorkflow"]
