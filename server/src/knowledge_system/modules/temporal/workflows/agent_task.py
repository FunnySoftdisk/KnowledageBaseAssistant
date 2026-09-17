"""DEV-03B：Root `AgentTaskWorkflow`可靠执行骨架。

只承载确定性调度、Signal、Query、截止时间与取消；LLM/检索/数据库/Tool一律进Activity。
首Run处理START；Continue-As-New输入契约已在DEV-03A冻结，处理逻辑归DEV-03E。

取消双路径收敛：业务取消以`TASK_CANCELLED`事件经事件游标识别并终态CAS；Temporal级取消
以`CancelledError`传播并由Temporal标记CANCELLED，PG终态由控制面在DEV-03C对账。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from temporalio import workflow
from temporalio.exceptions import ApplicationError

from knowledge_system.modules.temporal.domain.activity_profiles import (
    ActivityProfileKindV1,
    activity_profile,
)
from knowledge_system.modules.temporal.domain.contracts import (
    AgentRegistrationKeyV1,
    AgentTaskRunStateV1,
    AgentTaskRuntimeStateV1,
    AgentTaskWorkflowArgsV1,
    AgentTaskWorkflowContinueV1,
    AgentTaskWorkflowResultV1,
    AgentTaskWorkflowStartV1,
    ApplyBusinessEventsCommandV1,
    ApplyBusinessEventsResultV1,
    RootTerminalCommitV1,
)
from knowledge_system.modules.temporal.domain.topology import CONTROL_TASK_QUEUE
from knowledge_system.modules.temporal.domain.workflow_ids import (
    BUSINESS_EVENT_SIGNAL,
    RUNTIME_STATE_QUERY,
)
from knowledge_system.modules.temporal.workflows.control_activities import (
    APPLY_BUSINESS_EVENTS_ACTIVITY,
    COMMIT_ROOT_TERMINAL_STATE_ACTIVITY,
)
from knowledge_system.modules.temporal.workflows.profiles import (
    heartbeat_timeout,
    retry_policy,
)

_TASK_DEADLINE_EXCEEDED = "TASK_DEADLINE_EXCEEDED"


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
        self._attempt_kind = None
        self._agent_key = AgentRegistrationKeyV1.SUPERVISOR
        self._needs_reconcile = False
        self._events_pending = False

    @workflow.run
    async def run(self, args: AgentTaskWorkflowArgsV1) -> AgentTaskWorkflowResultV1:
        command = args.root
        if isinstance(command, AgentTaskWorkflowStartV1):
            return await self._run_start(command)
        if isinstance(command, AgentTaskWorkflowContinueV1):
            # Continue-As-New处理归DEV-03E；本批只冻结输入契约，不伪装已实现。
            raise ApplicationError(
                "CONTINUE_NOT_IMPLEMENTED_IN_DEV03B",
                type="CONTINUE_NOT_IMPLEMENTED_IN_DEV03B",
                non_retryable=True,
            )
        raise ApplicationError(
            "UNKNOWN_WORKFLOW_COMMAND", type="UNKNOWN_WORKFLOW_COMMAND", non_retryable=True
        )

    async def _run_start(self, start: AgentTaskWorkflowStartV1) -> AgentTaskWorkflowResultV1:
        self._task_id = start.task.task_id
        self._run_sequence = start.run_sequence
        self._deadline = start.budget.wall_clock_deadline
        self._event_cursor = start.task_created_event.sequence
        self._business_cursor = start.task_created_event.sequence
        self._run_state = AgentTaskRunStateV1.RUNNING

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

            now = workflow.now()
            if now >= self._deadline:
                return await self._commit_terminal(
                    AgentTaskRunStateV1.FAILED, _TASK_DEADLINE_EXCEEDED
                )

            remaining = (self._deadline - now).total_seconds()
            # Signal或截止时间唤醒；`wait_condition`带timeout时超时即抛`TimeoutError`，
            # 需捕获后回循环顶重新检查截止时间，否则异常会漏出`run`使Workflow失败。
            try:
                await workflow.wait_condition(
                    lambda: self._events_pending, timeout=remaining
                )
            except TimeoutError:
                continue

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

    profile = activity_profile(ActivityProfileKindV1.CONTROL_TRANSACTION)
    return {
        "task_queue": CONTROL_TASK_QUEUE,
        "start_to_close_timeout": timedelta(seconds=profile.start_to_close_seconds),
        "schedule_to_close_timeout": timedelta(seconds=profile.schedule_to_close_seconds),
        "heartbeat_timeout": heartbeat_timeout(profile),
        "retry_policy": retry_policy(profile),
    }


__all__ = ["AgentTaskWorkflow"]
