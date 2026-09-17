"""DEV-03B：Temporal Worker工厂与Outbox轮询器。

Worker按队列拆分：Workflow Worker只承载确定性`AgentTaskWorkflow`，Control Worker承载
PG事件游标/终态CAS Activity。五个队列按需扩缩，本批先起Workflow与Control两个。
"""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from knowledge_system.modules.temporal.application.outbox_publisher import (
    OutboxPollOutcome,
    OutboxPublisher,
)
from knowledge_system.modules.temporal.domain.topology import (
    CONTROL_TASK_QUEUE,
    WORKFLOW_TASK_QUEUE,
)
from knowledge_system.modules.temporal.workflows.agent_task import AgentTaskWorkflow
from knowledge_system.modules.temporal.workflows.control_activities import ControlActivities

logger = logging.getLogger(__name__)


class TemporalWorkerFactory:
    """由`temporalio.client.Client`构建本批两个Worker，注入已装配的Control Activity实例。"""

    def __init__(
        self,
        client: Client,
        control_activities: ControlActivities,
        *,
        workflow_task_queue: str = WORKFLOW_TASK_QUEUE,
        control_task_queue: str = CONTROL_TASK_QUEUE,
    ) -> None:
        self._client = client
        self._control_activities = control_activities
        self._workflow_task_queue = workflow_task_queue
        self._control_task_queue = control_task_queue

    def build(self) -> list[Worker]:
        workflow_worker = Worker(
            self._client,
            task_queue=self._workflow_task_queue,
            workflows=[AgentTaskWorkflow],
        )
        control_worker = Worker(
            self._client,
            task_queue=self._control_task_queue,
            activities=[
                self._control_activities.apply_business_events,
                self._control_activities.commit_root_terminal_state,
            ],
        )
        return [workflow_worker, control_worker]


class OutboxPoller:
    """循环消费`TEMPORAL_START`发件行；单轮失败不中断循环，`stop_event`/任务取消均能退出。"""

    def __init__(
        self, publisher: OutboxPublisher, *, poll_interval_seconds: float = 1.0
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("POLL_INTERVAL_SECONDS_INVALID")
        self._publisher = publisher
        self._poll_interval_seconds = poll_interval_seconds

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            try:
                outcome: OutboxPollOutcome = await self._publisher.poll_once()
                if outcome.claimed:
                    logger.info(
                        "outbox poll claimed=%d published=%d retried=%d dead=%d",
                        outcome.claimed,
                        outcome.published,
                        outcome.retried,
                        outcome.dead,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 轮询循环必须吞掉并继续
                logger.exception("outbox poll cycle failed")
            await self._sleep(stop_event)

    async def _sleep(self, stop_event: asyncio.Event | None) -> None:
        if stop_event is None:
            await asyncio.sleep(self._poll_interval_seconds)
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval_seconds)
        except TimeoutError:
            pass


__all__ = ["OutboxPoller", "TemporalWorkerFactory"]
