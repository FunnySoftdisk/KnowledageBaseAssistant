"""DEV-03A契约离线验收：冻结的Workflow输入输出、Workflow ID、队列、Profile与重试。

不连接Temporal/PG，只验证契约的判别、校验器、JSON往返与数值冻结；这些是DEV-03B/C/D/E
的共同地基，先于实现被锁定。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

from pydantic import ValidationError
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from knowledge_system.modules.temporal.application.temporal_client import (
    TemporalClientAdapter,
)
from knowledge_system.modules.temporal.domain.activity_profiles import (
    ACTIVITY_PROFILES_V1,
    ActivityProfileKindV1,
    activity_profile,
)
from knowledge_system.modules.temporal.domain.contracts import (
    AgentTaskRootStateV1,
    AgentTaskRunStateV1,
    AgentTaskWorkflowArgsV1,
    AgentTaskWorkflowContinueV1,
    AgentTaskWorkflowResultV1,
    AgentTaskWorkflowStartV1,
    BudgetUsageEntryV1,
    ContextVersionRefV1,
    PendingBusinessRequestRefV1,
    RootTerminalCommitV1,
    RuntimeContractRefV1,
    TaskBudgetV1,
    TaskEventRefV1,
    TaskRefV1,
)
from knowledge_system.modules.temporal.domain.topology import (
    AGENT_TASK_QUEUES,
    SEARCH_ATTRIBUTES,
    WORKFLOW_TASK_TIMEOUT_SECONDS,
    build_id,
)
from knowledge_system.modules.temporal.domain.workflow_ids import (
    agent_attempt_workflow_id,
    agent_task_workflow_id,
)
from knowledge_system.modules.temporal.workflows.profiles import (
    heartbeat_timeout,
    retry_policy,
)


def _start_args(
    task_id: object,
    event_id: object,
    deadline: datetime,
    *,
    run_sequence: int = 1,
) -> AgentTaskWorkflowArgsV1:
    return AgentTaskWorkflowArgsV1(
        AgentTaskWorkflowStartV1(
            task=TaskRefV1(task_id=task_id),
            task_created_event=TaskEventRefV1(event_id=event_id, sequence=1),
            runtime_contract=RuntimeContractRefV1(),
            budget=TaskBudgetV1(wall_clock_deadline=deadline),
            run_sequence=run_sequence,
        )
    )


class Dev03ContractTests(unittest.IsolatedAsyncioTestCase):
    def _deadline(self) -> datetime:
        return datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)

    async def test_start_contract_roundtrips_via_json(self) -> None:
        from temporalio.contrib.pydantic import pydantic_data_converter

        task_id, event_id = uuid4(), uuid4()
        args = _start_args(task_id, event_id, self._deadline())
        payloads = await pydantic_data_converter.encode([args])
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0].metadata["encoding"].decode(), "json/plain")
        (restored,) = await pydantic_data_converter.decode(payloads, [AgentTaskWorkflowArgsV1])
        self.assertIsInstance(restored.root, AgentTaskWorkflowStartV1)
        self.assertEqual(restored.root.task.task_id, task_id)
        self.assertEqual(restored.root.task_created_event.event_id, event_id)
        self.assertEqual(restored.root.task_created_event.sequence, 1)
        self.assertEqual(restored.root.budget.wall_clock_deadline, self._deadline())

    def test_start_requires_first_run_sequence(self) -> None:
        with self.assertRaises(ValidationError):
            _start_args(uuid4(), uuid4(), self._deadline(), run_sequence=2)

    def test_continue_requires_equal_dual_cursors(self) -> None:
        task_id = uuid4()
        base = {
            "task": TaskRefV1(task_id=task_id),
            "root_state": AgentTaskRootStateV1(
                event_cursor=1,
                business_cursor=2,
                run_state=AgentTaskRunStateV1.RUNNING,
            ),
            "budget": TaskBudgetV1(wall_clock_deadline=self._deadline()),
            "run_sequence": 2,
        }
        with self.assertRaises(ValidationError):
            AgentTaskWorkflowContinueV1(**base)
        base["root_state"] = AgentTaskRootStateV1(
            event_cursor=3,
            business_cursor=3,
            run_state=AgentTaskRunStateV1.RUNNING,
        )
        continue_args = AgentTaskWorkflowContinueV1(**base)
        self.assertEqual(continue_args.run_sequence, 2)

    def test_continue_collections_are_unique_and_sorted(self) -> None:
        root_state = AgentTaskRootStateV1(
            event_cursor=3,
            business_cursor=3,
            run_state=AgentTaskRunStateV1.RUNNING,
        )
        common = {
            "task": TaskRefV1(task_id=uuid4()),
            "root_state": root_state,
            "budget": TaskBudgetV1(wall_clock_deadline=self._deadline()),
            "run_sequence": 2,
        }
        with self.assertRaisesRegex(ValidationError, "CONTINUE_BUDGET_USED"):
            AgentTaskWorkflowContinueV1(
                **common,
                budget_used=(
                    BudgetUsageEntryV1(counter="model_calls", used=1),
                    BudgetUsageEntryV1(counter="capability_calls", used=1),
                ),
            )
        request_id = uuid4()
        with self.assertRaisesRegex(ValidationError, "CONTINUE_PENDING_REQUESTS"):
            AgentTaskWorkflowContinueV1(
                **common,
                pending_requests=(
                    PendingBusinessRequestRefV1(request_id=request_id, request_kind="APPROVAL"),
                    PendingBusinessRequestRefV1(request_id=request_id, request_kind="APPROVAL"),
                ),
            )
        context = ContextVersionRefV1(context_kind="TASK_CONTEXT", version=1, digest="a" * 64)
        with self.assertRaisesRegex(ValidationError, "CONTINUE_CONTEXT_VERSIONS"):
            AgentTaskWorkflowContinueV1(
                **common,
                context_versions=(context, context),
            )

    def test_budget_deadline_requires_utc_and_normalizes(self) -> None:
        with self.assertRaises(ValidationError):
            TaskBudgetV1(wall_clock_deadline=datetime(2030, 1, 1, 0, 0, 0))
        budget = TaskBudgetV1(wall_clock_deadline=datetime(2030, 1, 1, 8, 0, 0, tzinfo=UTC))
        self.assertEqual(budget.wall_clock_deadline, datetime(2030, 1, 1, 8, 0, 0, tzinfo=UTC))

    def test_workflow_result_is_terminal_only(self) -> None:
        for terminal in (
            AgentTaskRunStateV1.COMPLETED,
            AgentTaskRunStateV1.FAILED,
            AgentTaskRunStateV1.CANCELLED,
        ):
            with self.subTest(terminal=terminal):
                result = AgentTaskWorkflowResultV1(
                    task_id=uuid4(), run_sequence=1, terminal_state=terminal
                )
                self.assertEqual(result.terminal_state, terminal)
        with self.assertRaises(ValidationError):
            AgentTaskWorkflowResultV1(
                task_id=uuid4(),
                run_sequence=1,
                terminal_state=AgentTaskRunStateV1.RUNNING,
            )

    def test_root_terminal_commit_is_fail_or_cancel_only(self) -> None:
        for terminal in (AgentTaskRunStateV1.FAILED, AgentTaskRunStateV1.CANCELLED):
            with self.subTest(terminal=terminal):
                commit = RootTerminalCommitV1(
                    task_id=uuid4(), run_sequence=1, terminal_state=terminal
                )
                self.assertEqual(commit.terminal_state, terminal)
        with self.assertRaises(ValidationError):
            RootTerminalCommitV1(
                task_id=uuid4(),
                run_sequence=1,
                terminal_state=AgentTaskRunStateV1.COMPLETED,
            )

    def test_workflow_id_formats_are_deterministic(self) -> None:
        task_id, attempt_id = uuid4(), uuid4()
        self.assertEqual(agent_task_workflow_id(task_id), f"agent-task-v1-{task_id}")
        self.assertEqual(
            agent_attempt_workflow_id(task_id, attempt_id),
            f"agent-attempt-v1-{task_id}-{attempt_id}",
        )

    def test_activity_profiles_frozen_and_mapped(self) -> None:
        self.assertEqual(len(ACTIVITY_PROFILES_V1), 8)
        uncertain = activity_profile(ActivityProfileKindV1.CAPABILITY_EFFECT_UNCERTAIN)
        self.assertIsNone(uncertain.retry)
        self.assertEqual(uncertain.max_attempts, 1)
        for kind, profile in ACTIVITY_PROFILES_V1.items():
            with self.subTest(kind=kind):
                self.assertGreaterEqual(profile.max_attempts, 1)
                if profile.retry is not None:
                    retry = retry_policy(profile)
                    assert retry is not None
                    self.assertEqual(retry.maximum_attempts, profile.max_attempts)
                else:
                    self.assertIsNone(retry_policy(profile))

    def test_heartbeat_timeout_respects_profile(self) -> None:
        self.assertIsNone(
            heartbeat_timeout(activity_profile(ActivityProfileKindV1.CONTROL_TRANSACTION))
        )
        self.assertEqual(
            heartbeat_timeout(activity_profile(ActivityProfileKindV1.MODEL_REQUEST)),
            timedelta(seconds=30),
        )

    def test_topology_queues_and_build_id(self) -> None:
        self.assertEqual(len(AGENT_TASK_QUEUES), 5)
        self.assertEqual(AGENT_TASK_QUEUES[0], "knowledge.agent.workflow.v1")
        self.assertEqual(build_id("0.1.0", "abc1234"), "0.1.0+abc1234")
        self.assertEqual(WORKFLOW_TASK_TIMEOUT_SECONDS, 10)
        self.assertEqual(
            set(SEARCH_ATTRIBUTES),
            {
                "AgentRuntimeContract",
                "AgentWorkflowKind",
                "AgentRunState",
                "AgentAttemptKind",
                "AgentRegistrationKey",
                "AgentDeadline",
                "AgentNeedsReconcile",
                "AgentRunSequence",
            },
        )

    async def test_temporal_start_uses_frozen_id_and_timeout_policies(self) -> None:
        client = SimpleNamespace(start_workflow=AsyncMock())
        client.start_workflow.return_value = SimpleNamespace(result_run_id="run-1")
        adapter = TemporalClientAdapter(
            cast(Any, client), workflow_task_queue="knowledge.agent.workflow.v1"
        )
        args = _start_args(uuid4(), uuid4(), self._deadline())

        run_id = await adapter.start_task_workflow("agent-task-v1-test", args)

        self.assertEqual(run_id, "run-1")
        call = client.start_workflow.await_args
        self.assertEqual(call.kwargs["task_timeout"], timedelta(seconds=10))
        self.assertEqual(call.kwargs["id_conflict_policy"], WorkflowIDConflictPolicy.USE_EXISTING)
        self.assertEqual(call.kwargs["id_reuse_policy"], WorkflowIDReusePolicy.REJECT_DUPLICATE)


if __name__ == "__main__":
    unittest.main()
