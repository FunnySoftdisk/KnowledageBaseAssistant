"""DEV-03D Payload Codec的temporalio dev server验收。

以加密DataConverter启动dev server与Worker，跑通真实`AgentTaskWorkflow`并读取原始
History：证明Workflow输入/结果确实以`binary/encrypted`（AES-256-GCM）落History而非
`json/plain`明文，且加密后Workflow仍正常确定性完成（编解码闭环）。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from knowledge_system.modules.temporal.domain.contracts import (
    AgentTaskRunStateV1,
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
from knowledge_system.modules.temporal.domain.workflow_ids import agent_task_workflow_id
from knowledge_system.modules.temporal.payload import (
    ENCRYPTED_ENCODING,
    EncryptionPayloadCodec,
    InMemoryPayloadKeyProvider,
    build_data_converter,
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
    return ApplyBusinessEventsResultV1(
        task_id=command.task_id,
        applied_count=0,
        new_cursor=command.from_sequence,
        terminal_requested=None,
    )


@activity.defn(name=COMMIT_ROOT_TERMINAL_STATE_ACTIVITY)
async def _commit_terminal_stub(commit: RootTerminalCommitV1) -> None:
    _COMMITS.append(commit)


@activity.defn(name=CLAIM_NEXT_ATTEMPT_ACTIVITY)
async def _claim_none_stub(task_id: object) -> None:
    return None


def _start_args(deadline: datetime) -> AgentTaskWorkflowArgsV1:
    start = AgentTaskWorkflowStartV1(
        task=TaskRefV1(task_id=uuid4()),
        task_created_event=TaskEventRefV1(event_id=uuid4(), sequence=1),
        runtime_contract=RuntimeContractRefV1(),
        budget=TaskBudgetV1(wall_clock_deadline=deadline),
        run_sequence=1,
    )
    return AgentTaskWorkflowArgsV1(start)


class Dev03PayloadIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        codec = EncryptionPayloadCodec(
            InMemoryPayloadKeyProvider(key_id="dev-server-key"),
            max_inline_bytes=64 * 1024,
        )
        self.env = await WorkflowEnvironment.start_local(
            data_converter=build_data_converter(codec)
        )

    async def asyncTearDown(self) -> None:
        await self.env.shutdown()

    async def test_workflow_completes_and_history_is_encrypted(self) -> None:
        _COMMITS.clear()
        args = _start_args(datetime.now(UTC) + timedelta(seconds=2))

        workflow_worker = Worker(
            self.env.client,
            task_queue=WORKFLOW_TASK_QUEUE,
            workflows=[AgentTaskWorkflow],
        )
        control_worker = Worker(
            self.env.client,
            task_queue=CONTROL_TASK_QUEUE,
            activities=[_apply_events_stub, _commit_terminal_stub, _claim_none_stub],
        )
        async with workflow_worker, control_worker:
            handle = await self.env.client.start_workflow(
                AgentTaskWorkflow.run,
                args,
                id=agent_task_workflow_id(args.root.task.task_id),
                task_queue=WORKFLOW_TASK_QUEUE,
            )
            result = AgentTaskWorkflowResultV1.model_validate(await handle.result())

        self.assertEqual(result.terminal_state, AgentTaskRunStateV1.FAILED)
        self.assertEqual(result.error_code, "TASK_DEADLINE_EXCEEDED")

        # 原始History中Workflow启动输入必须已加密（不是json/plain明文）。
        history = await handle.fetch_history()
        started = next(
            event.workflow_execution_started_event_attributes
            for event in history.events
            if event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED
        )
        self.assertGreaterEqual(len(started.input.payloads), 1)
        for payload in started.input.payloads:
            self.assertEqual(payload.metadata["encoding"], ENCRYPTED_ENCODING)
            self.assertEqual(payload.metadata["kid"], b"dev-server-key")
            self.assertNotIn(b"task_id", payload.data)


if __name__ == "__main__":
    unittest.main()
