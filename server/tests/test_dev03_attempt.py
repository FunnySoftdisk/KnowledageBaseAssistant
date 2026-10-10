"""DEV-03C Child `AgentAttemptWorkflow`与Root派发的temporalio.testing验收。

以真实dev server验证：Child认知→四重CAS终态提交闭环、旧Generation隔离（FENCED静默返回）、
Root以确定性Child ID + `ParentClosePolicy.TERMINATE` + `REJECT_DUPLICATE`派发Child、以及
Root→Child→Activity取消传播（无孤儿Child）。认知Activity在本批以显式测试替身替换。
"""

from __future__ import annotations

import asyncio
import threading
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from temporalio import activity
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import CancelledError as TemporalCancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from knowledge_system.modules.temporal.domain.contracts import (
    AgentAttemptWorkflowInputV1,
    AgentTaskRunStateV1,
    AgentTaskWorkflowArgsV1,
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
    MODEL_TASK_QUEUE,
    WORKFLOW_TASK_QUEUE,
)
from knowledge_system.modules.temporal.domain.workflow_ids import (
    agent_attempt_workflow_id,
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

# 模块级stub状态：每次测试前清空/重置，捕获Activity的真实命令与提交。
_APPLY_EVENTS_RESULTS: list[ApplyBusinessEventsResultV1] = []
_CLAIM_DISPATCHES: list[AttemptDispatchV1 | None] = []
_EXECUTE_RESULTS: list[ExecuteAttemptResultV1] = []
_COMMIT_OUTCOMES: list[AttemptCommitOutcomeV1] = []
_EXECUTED_COMMANDS: list[ExecuteAttemptCommandV1] = []
_ATTEMPT_COMMITS: list[AttemptTerminalCommitV1] = []
_ROOT_COMMITS: list[RootTerminalCommitV1] = []

# 取消传播的线程安全信号（Activity运行在Worker事件循环，测试在另一事件循环）。
_EXECUTE_STARTED = threading.Event()
_EXECUTE_CANCELLED = threading.Event()


@activity.defn(name=APPLY_BUSINESS_EVENTS_ACTIVITY)
async def _apply_events_stub(command: ApplyBusinessEventsCommandV1) -> ApplyBusinessEventsResultV1:
    if _APPLY_EVENTS_RESULTS:
        return _APPLY_EVENTS_RESULTS.pop(0)
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
async def _claim_stub(task_id: UUID) -> AttemptDispatchV1 | None:
    return _CLAIM_DISPATCHES.pop(0) if _CLAIM_DISPATCHES else None


@activity.defn(name=EXECUTE_ATTEMPT_ACTIVITY)
async def _execute_stub(command: ExecuteAttemptCommandV1) -> ExecuteAttemptResultV1:
    _EXECUTED_COMMANDS.append(command)
    if _EXECUTE_RESULTS:
        return _EXECUTE_RESULTS.pop(0)
    return ExecuteAttemptResultV1(
        task_id=command.task_id, task_attempt_id=command.task_attempt_id
    )


@activity.defn(name=EXECUTE_ATTEMPT_ACTIVITY)
async def _blocking_execute_stub(command: ExecuteAttemptCommandV1) -> ExecuteAttemptResultV1:
    _EXECUTE_STARTED.set()
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        _EXECUTE_CANCELLED.set()
        raise
    raise RuntimeError("unreachable")


@activity.defn(name=COMMIT_ATTEMPT_TERMINAL_STATE_ACTIVITY)
async def _commit_attempt_stub(commit: AttemptTerminalCommitV1) -> AttemptCommitOutcomeV1:
    _ATTEMPT_COMMITS.append(commit)
    if _COMMIT_OUTCOMES:
        return _COMMIT_OUTCOMES.pop(0)
    return AttemptCommitOutcomeV1(task_attempt_id=commit.task_attempt_id, committed=True)


def _start_args(deadline: datetime) -> tuple[AgentTaskWorkflowArgsV1, UUID]:
    task_id = uuid4()
    start = AgentTaskWorkflowStartV1(
        task=TaskRefV1(task_id=task_id),
        task_created_event=TaskEventRefV1(event_id=uuid4(), sequence=1),
        runtime_contract=RuntimeContractRefV1(),
        budget=TaskBudgetV1(wall_clock_deadline=deadline),
        run_sequence=1,
    )
    return AgentTaskWorkflowArgsV1(start), task_id


def _child_input(
    task_id: UUID, attempt_id: UUID, *, lease_generation: int, attempt_row_version: int
) -> AgentAttemptWorkflowInputV1:
    return AgentAttemptWorkflowInputV1(
        task_id=task_id,
        task_attempt_id=attempt_id,
        attempt_kind=AttemptKindV1.PLAN,
        plan_version=1,
        lease_generation=lease_generation,
        attempt_row_version=attempt_row_version,
        run_sequence=1,
        parent_run_sequence=1,
    )


class Dev03AttemptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.env = await WorkflowEnvironment.start_local(
            data_converter=pydantic_data_converter
        )

    async def asyncTearDown(self) -> None:
        await self.env.shutdown()

    async def _start_workers(self, *, blocking_execute: bool = False):
        execute = _blocking_execute_stub if blocking_execute else _execute_stub
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
        model_worker = Worker(
            self.env.client,
            task_queue=MODEL_TASK_QUEUE,
            activities=[execute],
        )
        return workflow_worker, control_worker, model_worker

    async def test_child_completes_and_commits_with_fencing_tokens(self) -> None:
        _EXECUTED_COMMANDS.clear()
        _ATTEMPT_COMMITS.clear()
        task_id, attempt_id = uuid4(), uuid4()
        _EXECUTE_RESULTS[:] = [
            ExecuteAttemptResultV1(
                task_id=task_id,
                task_attempt_id=attempt_id,
                result_kind="PLAN_RESULT",
                result_schema_version="v1",
                result_digest="b" * 64,
            )
        ]
        _COMMIT_OUTCOMES[:] = [
            AttemptCommitOutcomeV1(task_attempt_id=attempt_id, committed=True)
        ]
        attempt_input = _child_input(task_id, attempt_id, lease_generation=3, attempt_row_version=5)

        workflow_worker, control_worker, model_worker = await self._start_workers()
        async with workflow_worker, control_worker, model_worker:
            handle = await self.env.client.start_workflow(
                AgentAttemptWorkflow.run,
                attempt_input,
                id=agent_attempt_workflow_id(task_id, attempt_id),
                task_queue=WORKFLOW_TASK_QUEUE,
            )
            result = await handle.result()

        self.assertEqual(result.root.kind, "COMPLETED")
        self.assertEqual(len(_EXECUTED_COMMANDS), 1)
        self.assertEqual(_EXECUTED_COMMANDS[0].task_attempt_id, attempt_id)
        self.assertEqual(_EXECUTED_COMMANDS[0].attempt_kind, AttemptKindV1.PLAN)
        self.assertEqual(len(_ATTEMPT_COMMITS), 1)
        self.assertEqual(_ATTEMPT_COMMITS[0].task_attempt_id, attempt_id)
        self.assertEqual(_ATTEMPT_COMMITS[0].lease_generation, 3)
        self.assertEqual(_ATTEMPT_COMMITS[0].expected_row_version, 5)
        self.assertEqual(_ATTEMPT_COMMITS[0].terminal_state, "COMPLETED")
        self.assertEqual(_ATTEMPT_COMMITS[0].result_digest, "b" * 64)

    async def test_child_fenced_stale_generation_returns_failed(self) -> None:
        _EXECUTED_COMMANDS.clear()
        _ATTEMPT_COMMITS.clear()
        task_id, attempt_id = uuid4(), uuid4()
        _EXECUTE_RESULTS[:] = [
            ExecuteAttemptResultV1(
                task_id=task_id,
                task_attempt_id=attempt_id,
                result_kind="PLAN_RESULT",
                result_digest="c" * 64,
            )
        ]
        _COMMIT_OUTCOMES[:] = [
            AttemptCommitOutcomeV1(
                task_attempt_id=attempt_id,
                committed=False,
                conflict_reason="STALE_LEASE_GENERATION",
            )
        ]
        attempt_input = _child_input(task_id, attempt_id, lease_generation=3, attempt_row_version=5)

        workflow_worker, control_worker, model_worker = await self._start_workers()
        async with workflow_worker, control_worker, model_worker:
            handle = await self.env.client.start_workflow(
                AgentAttemptWorkflow.run,
                attempt_input,
                id=agent_attempt_workflow_id(task_id, attempt_id),
                task_queue=WORKFLOW_TASK_QUEUE,
            )
            result = await handle.result()

        self.assertEqual(result.root.kind, "FAILED")
        self.assertEqual(result.root.error_code, "ATTEMPT_FENCED_STALE_GENERATION")
        self.assertEqual(result.root.reason, "STALE_LEASE_GENERATION")
        self.assertEqual(len(_ATTEMPT_COMMITS), 1)

    async def test_root_dispatches_child_then_continues(self) -> None:
        _EXECUTED_COMMANDS.clear()
        _ATTEMPT_COMMITS.clear()
        args, task_id = _start_args(datetime.now(UTC) + timedelta(seconds=3))
        attempt_id = uuid4()
        _CLAIM_DISPATCHES[:] = [
            AttemptDispatchV1(
                task_attempt_id=attempt_id,
                attempt_kind=AttemptKindV1.PLAN,
                plan_version=1,
                lease_generation=1,
                attempt_row_version=2,
            )
        ]
        _EXECUTE_RESULTS[:] = [
            ExecuteAttemptResultV1(
                task_id=task_id,
                task_attempt_id=attempt_id,
                result_kind="PLAN_RESULT",
                result_digest="d" * 64,
            )
        ]
        _COMMIT_OUTCOMES[:] = [
            AttemptCommitOutcomeV1(task_attempt_id=attempt_id, committed=True)
        ]

        workflow_worker, control_worker, model_worker = await self._start_workers()
        async with workflow_worker, control_worker, model_worker:
            handle = await self.env.client.start_workflow(
                AgentTaskWorkflow.run,
                args,
                id=agent_task_workflow_id(task_id),
                task_queue=WORKFLOW_TASK_QUEUE,
            )
            result = await handle.result()

        # 子流程完成后Root继续回环，认领为空→等待→截止时间到期→FAILED。
        self.assertEqual(result.terminal_state, AgentTaskRunStateV1.FAILED)
        self.assertEqual(result.error_code, "TASK_DEADLINE_EXCEEDED")
        self.assertEqual(len(_EXECUTED_COMMANDS), 1)
        self.assertEqual(_EXECUTED_COMMANDS[0].task_id, task_id)
        self.assertEqual(_EXECUTED_COMMANDS[0].task_attempt_id, attempt_id)
        self.assertEqual(len(_ATTEMPT_COMMITS), 1)
        self.assertEqual(_ATTEMPT_COMMITS[0].terminal_state, "COMPLETED")
        self.assertEqual(_ATTEMPT_COMMITS[0].lease_generation, 1)
        self.assertEqual(_ATTEMPT_COMMITS[0].expected_row_version, 2)

    async def test_cancel_root_propagates_to_child_activity(self) -> None:
        _ROOT_COMMITS.clear()
        _EXECUTE_STARTED.clear()
        _EXECUTE_CANCELLED.clear()
        args, task_id = _start_args(datetime.now(UTC) + timedelta(seconds=60))
        attempt_id = uuid4()
        _CLAIM_DISPATCHES[:] = [
            AttemptDispatchV1(
                task_attempt_id=attempt_id,
                attempt_kind=AttemptKindV1.PLAN,
                plan_version=1,
                lease_generation=1,
                attempt_row_version=2,
            )
        ]

        workflow_worker, control_worker, model_worker = await self._start_workers(
            blocking_execute=True
        )
        async with workflow_worker, control_worker, model_worker:
            handle = await self.env.client.start_workflow(
                AgentTaskWorkflow.run,
                args,
                id=agent_task_workflow_id(task_id),
                task_queue=WORKFLOW_TASK_QUEUE,
            )
            for _ in range(100):
                if _EXECUTE_STARTED.is_set():
                    break
                await asyncio.sleep(0.1)
            self.assertTrue(_EXECUTE_STARTED.is_set(), "child did not enter execute activity")

            await handle.cancel()
            with self.assertRaises(WorkflowFailureError) as ctx:
                await handle.result()
            self.assertIsInstance(ctx.exception.cause, TemporalCancelledError)

        # Root→Child→Activity取消传播：阻塞的认知Activity被取消。
        self.assertTrue(_EXECUTE_CANCELLED.is_set(), "blocking activity was not cancelled")
        # Root在取消路径把终态CANCELLED落PG。
        self.assertEqual(_ROOT_COMMITS[-1].terminal_state, AgentTaskRunStateV1.CANCELLED)
        self.assertEqual(_ROOT_COMMITS[-1].error_code, "TASK_CANCELLED_BY_TEMPORAL")
        # 无孤儿Child：Child以CANCELLED或TERMINATED结束，不再是RUNNING。
        child_handle = self.env.client.get_workflow_handle(
            agent_attempt_workflow_id(task_id, attempt_id)
        )
        description = await child_handle.describe()
        self.assertIn(
            description.status,
            (WorkflowExecutionStatus.CANCELED, WorkflowExecutionStatus.TERMINATED),
        )


if __name__ == "__main__":
    unittest.main()
