"""DEV-03E Continue-As-New与CONTINUE续跑输入的temporalio.testing验收。

以真实dev server验证：
1. CONTINUE输入：Root以`AgentTaskWorkflowContinueV1`直接启动，恢复运行态/预算/游标后进入
   同一主循环，终态提交与结果的`run_sequence`均来自续跑序号；
2. Continue-As-New：单Run派发满`MAX_ATTEMPTS_PER_ROOT_RUN`次Attempt后确定性续跑，首Run
   History含`WorkflowExecutionContinuedAsNew`事件、续跑序号+1、续跑Run独立落在终态。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from knowledge_system.modules.temporal.domain.contracts import (
    AgentTaskRootStateV1,
    AgentTaskRunStateV1,
    AgentTaskRuntimeStateV1,
    AgentTaskWorkflowArgsV1,
    AgentTaskWorkflowContinueV1,
    AgentTaskWorkflowResultV1,
    AgentTaskWorkflowStartV1,
    ApplyBusinessEventsCommandV1,
    ApplyBusinessEventsResultV1,
    AttemptCommitOutcomeV1,
    AttemptDispatchV1,
    AttemptKindV1,
    AttemptTerminalCommitV1,
    ExecuteAttemptCommandV1,
    ExecuteAttemptResultV1,
    RootTerminalCommitV1,
    RuntimeContractRefV1,
    TaskBudgetV1,
    TaskEventRefV1,
    TaskRefV1,
)
from knowledge_system.modules.temporal.domain.topology import (
    CONTROL_TASK_QUEUE,
    MAX_ATTEMPTS_PER_ROOT_RUN,
    MODEL_TASK_QUEUE,
    WORKFLOW_TASK_QUEUE,
)
from knowledge_system.modules.temporal.domain.workflow_ids import (
    RUNTIME_STATE_QUERY,
    agent_task_workflow_id,
)
from knowledge_system.modules.temporal.workflows.agent_attempt import AgentAttemptWorkflow
from knowledge_system.modules.temporal.workflows.agent_task import AgentTaskWorkflow
from knowledge_system.modules.temporal.workflows.attempt_activities import (
    CLAIM_NEXT_ATTEMPT_ACTIVITY,
    COMMIT_ATTEMPT_TERMINAL_STATE_ACTIVITY,
    EXECUTE_ATTEMPT_ACTIVITY,
)
from knowledge_system.modules.temporal.workflows.control_activities import (
    APPLY_BUSINESS_EVENTS_ACTIVITY,
    COMMIT_ROOT_TERMINAL_STATE_ACTIVITY,
)

# 模块级stub状态：每次测试前清空，捕获控制面Activity的真实提交与认领结果。
_CLAIM_DISPATCHES: list[AttemptDispatchV1 | None] = []
_ROOT_COMMITS: list[RootTerminalCommitV1] = []


@activity.defn(name=APPLY_BUSINESS_EVENTS_ACTIVITY)
async def _apply_events_stub(command: ApplyBusinessEventsCommandV1) -> ApplyBusinessEventsResultV1:
    return ApplyBusinessEventsResultV1(
        task_id=command.task_id,
        applied_count=0,
        new_cursor=command.from_sequence,
        terminal_requested=None,
    )


@activity.defn(name=COMMIT_ROOT_TERMINAL_STATE_ACTIVITY)
async def _commit_root_stub(commit: RootTerminalCommitV1) -> None:
    _ROOT_COMMITS.append(commit)


@activity.defn(name=CLAIM_NEXT_ATTEMPT_ACTIVITY)
async def _claim_stub(task_id: object) -> AttemptDispatchV1 | None:
    return _CLAIM_DISPATCHES.pop(0) if _CLAIM_DISPATCHES else None


@activity.defn(name=EXECUTE_ATTEMPT_ACTIVITY)
async def _execute_stub(command: ExecuteAttemptCommandV1) -> ExecuteAttemptResultV1:
    return ExecuteAttemptResultV1(
        task_id=command.task_id, task_attempt_id=command.task_attempt_id
    )


@activity.defn(name=COMMIT_ATTEMPT_TERMINAL_STATE_ACTIVITY)
async def _commit_attempt_stub(commit: AttemptTerminalCommitV1) -> AttemptCommitOutcomeV1:
    return AttemptCommitOutcomeV1(task_attempt_id=commit.task_attempt_id, committed=True)


def _start_args(
    task_id: object, deadline: datetime
) -> AgentTaskWorkflowArgsV1:
    start = AgentTaskWorkflowStartV1(
        task=TaskRefV1(task_id=task_id),
        task_created_event=TaskEventRefV1(event_id=uuid4(), sequence=1),
        runtime_contract=RuntimeContractRefV1(),
        budget=TaskBudgetV1(wall_clock_deadline=deadline),
        run_sequence=1,
    )
    return AgentTaskWorkflowArgsV1(start)


class Dev03ContinueAsNewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.env = await WorkflowEnvironment.start_local(
            data_converter=pydantic_data_converter
        )

    async def asyncTearDown(self) -> None:
        await self.env.shutdown()

    async def _start_workers(self, *, with_model: bool):
        workflow_worker = Worker(
            self.env.client,
            task_queue=WORKFLOW_TASK_QUEUE,
            workflows=[AgentTaskWorkflow, AgentAttemptWorkflow],
        )
        control_worker = Worker(
            self.env.client,
            task_queue=CONTROL_TASK_QUEUE,
            activities=[
                _apply_events_stub,
                _commit_root_stub,
                _claim_stub,
                _commit_attempt_stub,
            ],
        )
        if not with_model:
            return workflow_worker, control_worker
        model_worker = Worker(
            self.env.client,
            task_queue=MODEL_TASK_QUEUE,
            activities=[_execute_stub],
        )
        return workflow_worker, control_worker, model_worker

    async def test_continue_input_restores_state(self) -> None:
        _CLAIM_DISPATCHES.clear()
        _ROOT_COMMITS.clear()
        task_id = uuid4()
        deadline = datetime.now(UTC) + timedelta(seconds=3)
        continue_args = AgentTaskWorkflowArgsV1(
            AgentTaskWorkflowContinueV1(
                task=TaskRefV1(task_id=task_id),
                root_state=AgentTaskRootStateV1(
                    event_cursor=5,
                    business_cursor=5,
                    run_state=AgentTaskRunStateV1.RUNNING,
                    attempt_kind=AttemptKindV1.PLAN,
                ),
                budget=TaskBudgetV1(wall_clock_deadline=deadline),
                run_sequence=3,
            )
        )

        workflow_worker, control_worker = await self._start_workers(with_model=False)
        async with workflow_worker, control_worker:
            handle = await self.env.client.start_workflow(
                AgentTaskWorkflow.run,
                continue_args,
                id=agent_task_workflow_id(task_id),
                task_queue=WORKFLOW_TASK_QUEUE,
            )
            state = await handle.query(
                RUNTIME_STATE_QUERY, result_type=AgentTaskRuntimeStateV1
            )
            self.assertEqual(state.run_sequence, 3)
            self.assertEqual(state.event_cursor, 5)
            self.assertEqual(state.business_cursor, 5)
            self.assertEqual(state.attempt_kind, AttemptKindV1.PLAN)
            result = AgentTaskWorkflowResultV1.model_validate(await handle.result())

        self.assertEqual(result.terminal_state, AgentTaskRunStateV1.FAILED)
        self.assertEqual(result.error_code, "TASK_DEADLINE_EXCEEDED")
        self.assertEqual(result.run_sequence, 3)
        self.assertEqual(_ROOT_COMMITS[-1].run_sequence, 3)

    async def test_continue_as_new_after_max_attempts(self) -> None:
        _CLAIM_DISPATCHES.clear()
        _ROOT_COMMITS.clear()
        task_id = uuid4()
        # Deadline只作续跑后Run 2的终态触发，须足够宽裕以免在首Run派发满
        # `MAX_ATTEMPTS_PER_ROOT_RUN`次Attempt前就到期（in-memory dev server下每次Attempt
        # 约1s，10次≈10s，10s原值在负载下会先于Continue-As-New触发导致run_sequence=1）。
        deadline = datetime.now(UTC) + timedelta(seconds=30)
        args = _start_args(task_id, deadline)
        _CLAIM_DISPATCHES[:] = [
            AttemptDispatchV1(
                task_attempt_id=uuid4(),
                attempt_kind=AttemptKindV1.PLAN,
                plan_version=1,
                lease_generation=1,
                attempt_row_version=2,
            )
            for _ in range(MAX_ATTEMPTS_PER_ROOT_RUN)
        ]

        workflow_worker, control_worker, model_worker = await self._start_workers(
            with_model=True
        )
        async with workflow_worker, control_worker, model_worker:
            handle = await self.env.client.start_workflow(
                AgentTaskWorkflow.run,
                args,
                id=agent_task_workflow_id(task_id),
                task_queue=WORKFLOW_TASK_QUEUE,
            )
            result = AgentTaskWorkflowResultV1.model_validate(await handle.result())

        # 续跑序号+1：终态结果与终态提交都来自续跑Run。
        self.assertEqual(result.terminal_state, AgentTaskRunStateV1.FAILED)
        self.assertEqual(result.error_code, "TASK_DEADLINE_EXCEEDED")
        self.assertEqual(result.run_sequence, 2)
        self.assertEqual(_ROOT_COMMITS[-1].run_sequence, 2)
        # 首Run History含Continue-As-New事件，续跑确为新Run。
        first_handle = self.env.client.get_workflow_handle(
            agent_task_workflow_id(task_id), run_id=handle.first_execution_run_id
        )
        first_history = await first_handle.fetch_history()
        self.assertTrue(
            any(
                event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW
                for event in first_history.events
            )
        )


if __name__ == "__main__":
    unittest.main()
