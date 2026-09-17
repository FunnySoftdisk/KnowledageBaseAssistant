"""DEV-03B：Temporal客户端端口与真实适配器。

Root显式使用`WorkflowIDConflictPolicy.USE_EXISTING + WorkflowIDReusePolicy.REJECT_DUPLICATE`：
重复启动返回既有运行，禁止`TERMINATE_EXISTING`或换ID绕过。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Protocol

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from knowledge_system.modules.temporal.domain.contracts import AgentTaskWorkflowArgsV1
from knowledge_system.modules.temporal.domain.topology import WORKFLOW_TASK_TIMEOUT_SECONDS
from knowledge_system.modules.temporal.workflows.agent_task import AgentTaskWorkflow


class TemporalWorkflowStarter(Protocol):
    """启动Root Workflow的端口；测试用Fake替换，不虚构真实Temporal连接。"""

    async def start_task_workflow(self, workflow_id: str, args: AgentTaskWorkflowArgsV1) -> str:
        """幂等启动Root；返回现有或新建的Run ID。"""
        ...


class TemporalClientAdapter:
    """真实`temporalio.client.Client`适配器。"""

    def __init__(self, client: Client, *, workflow_task_queue: str) -> None:
        self._client = client
        self._workflow_task_queue = workflow_task_queue

    async def start_task_workflow(self, workflow_id: str, args: AgentTaskWorkflowArgsV1) -> str:
        handle = await self._client.start_workflow(
            AgentTaskWorkflow.run,
            args,
            id=workflow_id,
            task_queue=self._workflow_task_queue,
            task_timeout=timedelta(seconds=WORKFLOW_TASK_TIMEOUT_SECONDS),
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        run_id = handle.result_run_id
        if run_id is None:
            raise RuntimeError("TEMPORAL_START_MISSING_RUN_ID")
        return run_id


__all__ = ["TemporalClientAdapter", "TemporalWorkflowStarter"]
