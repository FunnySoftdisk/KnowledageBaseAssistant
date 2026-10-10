"""DEV-03E 真实Temporal重放与Worker重启的验收。

以真实dev server验证：
1. 重放：跑通一条完整History（截止失败路径），用`Replayer`按沙箱重放该History，断言无
   非确定性（`replay_failure is None`）；
2. 重启：Workflow在`wait_condition`停驻时关闭Worker，再以新Worker实例接续，Signal驱动
   Root提交终态，证明Workflow状态存于服务端、Worker重启不丢运行态。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

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
from knowledge_system.modules.temporal.workflows.attempt_activities import (
    CLAIM_NEXT_ATTEMPT_ACTIVITY,
)
from knowledge_system.modules.temporal.workflows.control_activities import (
    APPLY_BUSINESS_EVENTS_ACTIVITY,
    COMMIT_ROOT_TERMINAL_STATE_ACTIVITY,
)

_COMMITS: list[RootTerminalCommitV1] = []


@activity.defn(name=APPLY_BUSINESS_EVENTS_ACTIVITY)
async def _apply_events_stub(command: ApplyBusinessEventsCommandV1) -> ApplyBusinessEventsResultV1:
    # 本文件只在“重启后Signal驱动终态”路径调用它：返回CANCELLED终态。
    return ApplyBusinessEventsResultV1(
        task_id=command.task_id,
        applied_count=1,
        new_cursor=command.from_sequence + 1,
        terminal_requested="CANCELLED",
    )


@activity.defn(name=COMMIT_ROOT_TERMINAL_STATE_ACTIVITY)
async def _commit_terminal_stub(commit: RootTerminalCommitV1) -> None:
    _COMMITS.append(commit)


@activity.defn(name=CLAIM_NEXT_ATTEMPT_ACTIVITY)
async def _claim_none_stub(task_id: object) -> None:
    return None


def _start_args(task_id: object, deadline: datetime) -> AgentTaskWorkflowArgsV1:
    start = AgentTaskWorkflowStartV1(
        task=TaskRefV1(task_id=task_id),
        task_created_event=TaskEventRefV1(event_id=uuid4(), sequence=1),
        runtime_contract=RuntimeContractRefV1(),
        budget=TaskBudgetV1(wall_clock_deadline=deadline),
        run_sequence=1,
    )
    return AgentTaskWorkflowArgsV1(start)


class Dev03ReplayRestartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.env = await WorkflowEnvironment.start_local(
            data_converter=pydantic_data_converter
        )

    async def asyncTearDown(self) -> None:
        await self.env.shutdown()

    def _workflow_worker(self) -> Worker:
        return Worker(
            self.env.client,
            task_queue=WORKFLOW_TASK_QUEUE,
            workflows=[AgentTaskWorkflow],
        )

    def _control_worker(self) -> Worker:
        return Worker(
            self.env.client,
            task_queue=CONTROL_TASK_QUEUE,
            activities=[_apply_events_stub, _commit_terminal_stub, _claim_none_stub],
        )

    async def test_deadline_history_replays_deterministically(self) -> None:
        _COMMITS.clear()
        task_id = uuid4()
        args = _start_args(task_id, datetime.now(UTC) + timedelta(seconds=2))

        async with self._workflow_worker(), self._control_worker():
            handle = await self.env.client.start_workflow(
                AgentTaskWorkflow.run,
                args,
                id=agent_task_workflow_id(task_id),
                task_queue=WORKFLOW_TASK_QUEUE,
            )
            result = AgentTaskWorkflowResultV1.model_validate(await handle.result())
        self.assertEqual(result.terminal_state, AgentTaskRunStateV1.FAILED)

        # 真实History重放：按沙箱重放整条History，非确定性会以replay_failure浮现。
        history = await handle.fetch_history()
        replayer = Replayer(
            workflows=[AgentTaskWorkflow], data_converter=pydantic_data_converter
        )
        replay_result = await replayer.replay_workflow(history)
        self.assertIsNone(replay_result.replay_failure)

    async def test_worker_restart_resumes_workflow(self) -> None:
        _COMMITS.clear()
        task_id = uuid4()
        args = _start_args(task_id, datetime.now(UTC) + timedelta(seconds=60))

        # 第一代Worker：启动Workflow并确认其停驻于wait_condition（RUNNING、游标=1）。
        async with self._workflow_worker(), self._control_worker():
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

        # 第一代Worker已关闭（Workflow状态存于服务端）；第二代Worker接续同一队列。
        async with self._workflow_worker(), self._control_worker():
            await handle.signal(BUSINESS_EVENT_SIGNAL)
            result = AgentTaskWorkflowResultV1.model_validate(await handle.result())

        self.assertEqual(result.terminal_state, AgentTaskRunStateV1.CANCELLED)
        self.assertEqual(result.error_code, "TASK_CANCELLED")
        self.assertEqual(len(_COMMITS), 1)
        self.assertEqual(_COMMITS[0].terminal_state, AgentTaskRunStateV1.CANCELLED)
        self.assertEqual(_COMMITS[0].task_id, task_id)


if __name__ == "__main__":
    unittest.main()
