"""DEV-03B Root `AgentTaskWorkflow`的temporalio.testing环境验收。

以真实dev server（`start_local`）验证Root的确定性调度、Signal→游标推进→终态CAS、
以及截止时间到期失败路径。认知Activity在本批以明确测试替身（模块级stub）替换，
控制面Activity的语义由stub断言捕获；DEV-03C/D/E才接入真实Activity与真实Temporal故障面。

- 启动即RUNNING：`runtime_state_v1`查询返回RUNNING、游标=1、截止时间正确；
- Signal推进：`business_event_available_v1`唤醒主循环，stub返回终态CANCELLED，
  Root提交`TASK_CANCELLED`终态并返回CANCELLED结果；
- 截止时间：无Signal且截止到期，Root提交`TASK_DEADLINE_EXCEEDED`并返回FAILED。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from knowledge_system.modules.temporal.domain.contracts import (
    AgentTaskRunStateV1,
    AgentTaskRuntimeStateV1,
    AgentTaskWorkflowArgsV1,
    AgentTaskWorkflowResultV1,
    AgentTaskWorkflowStartV1,
    ApplyBusinessEventsCommandV1,
    ApplyBusinessEventsResultV1,
    RootTerminalCommitV1,
    RuntimeContractRefV1,
    TaskBudgetV1,
    TaskEventRefV1,
    TaskRefV1,
)
from knowledge_system.modules.temporal.domain.topology import (
    CONTROL_TASK_QUEUE,
    WORKFLOW_TASK_QUEUE,
)
from knowledge_system.modules.temporal.domain.workflow_ids import (
    BUSINESS_EVENT_SIGNAL,
    RUNTIME_STATE_QUERY,
    agent_task_workflow_id,
)
from knowledge_system.modules.temporal.workflows.agent_task import AgentTaskWorkflow
from knowledge_system.modules.temporal.workflows.control_activities import (
    APPLY_BUSINESS_EVENTS_ACTIVITY,
    COMMIT_ROOT_TERMINAL_STATE_ACTIVITY,
)

# 模块级stub状态：每次测试前清空，捕获控制面Activity的真实提交。
_COMMITS: list[RootTerminalCommitV1] = []


@activity.defn(name=APPLY_BUSINESS_EVENTS_ACTIVITY)
async def _apply_events_stub(command: ApplyBusinessEventsCommandV1) -> ApplyBusinessEventsResultV1:
    return ApplyBusinessEventsResultV1(
        task_id=command.task_id,
        applied_count=1,
        new_cursor=command.from_sequence + 1,
        terminal_requested="CANCELLED",
    )


@activity.defn(name=COMMIT_ROOT_TERMINAL_STATE_ACTIVITY)
async def _commit_terminal_stub(commit: RootTerminalCommitV1) -> None:
    _COMMITS.append(commit)


def _start_args(deadline: datetime) -> tuple[AgentTaskWorkflowArgsV1, object]:
    task_id = uuid4()
    event_id = uuid4()
    start = AgentTaskWorkflowStartV1(
        task=TaskRefV1(task_id=task_id),
        task_created_event=TaskEventRefV1(event_id=event_id, sequence=1),
        runtime_contract=RuntimeContractRefV1(),
        budget=TaskBudgetV1(wall_clock_deadline=deadline),
        run_sequence=1,
    )
    return AgentTaskWorkflowArgsV1(start), task_id


class Dev03WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.env = await WorkflowEnvironment.start_local(
            data_converter=pydantic_data_converter
        )

    async def asyncTearDown(self) -> None:
        await self.env.shutdown()

    async def _start_workers(self):
        workflow_worker = Worker(
            self.env.client,
            task_queue=WORKFLOW_TASK_QUEUE,
            workflows=[AgentTaskWorkflow],
        )
        control_worker = Worker(
            self.env.client,
            task_queue=CONTROL_TASK_QUEUE,
            activities=[_apply_events_stub, _commit_terminal_stub],
        )
        return workflow_worker, control_worker

    async def test_signal_advances_cursor_and_commits_cancel(self) -> None:
        _COMMITS.clear()
        deadline = datetime.now(UTC) + timedelta(seconds=30)
        args, task_id = _start_args(deadline)
        workflow_worker, control_worker = await self._start_workers()
        async with workflow_worker, control_worker:
            handle = await self.env.client.start_workflow(
                AgentTaskWorkflow.run,
                args,
                id=agent_task_workflow_id(task_id),
                task_queue=WORKFLOW_TASK_QUEUE,
            )
            state = await handle.query(
                RUNTIME_STATE_QUERY, result_type=AgentTaskRuntimeStateV1
            )
            self.assertEqual(state.run_state, AgentTaskRunStateV1.RUNNING)
            self.assertEqual(state.event_cursor, 1)
            self.assertEqual(state.business_cursor, 1)
            self.assertEqual(state.deadline, deadline)

            await handle.signal(BUSINESS_EVENT_SIGNAL)
            result = AgentTaskWorkflowResultV1.model_validate(await handle.result())

        self.assertEqual(result.terminal_state, AgentTaskRunStateV1.CANCELLED)
        self.assertEqual(result.error_code, "TASK_CANCELLED")
        self.assertEqual(len(_COMMITS), 1)
        self.assertEqual(_COMMITS[0].terminal_state, AgentTaskRunStateV1.CANCELLED)
        self.assertEqual(_COMMITS[0].task_id, task_id)

    async def test_deadline_exceeded_fails_root(self) -> None:
        _COMMITS.clear()
        deadline = datetime.now(UTC) + timedelta(seconds=2)
        args, task_id = _start_args(deadline)
        workflow_worker, control_worker = await self._start_workers()
        async with workflow_worker, control_worker:
            handle = await self.env.client.start_workflow(
                AgentTaskWorkflow.run,
                args,
                id=agent_task_workflow_id(task_id),
                task_queue=WORKFLOW_TASK_QUEUE,
            )
            result = AgentTaskWorkflowResultV1.model_validate(await handle.result())

        self.assertEqual(result.terminal_state, AgentTaskRunStateV1.FAILED)
        self.assertEqual(result.error_code, "TASK_DEADLINE_EXCEEDED")
        self.assertEqual(len(_COMMITS), 1)
        self.assertEqual(_COMMITS[0].terminal_state, AgentTaskRunStateV1.FAILED)
        self.assertEqual(_COMMITS[0].error_code, "TASK_DEADLINE_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
