"""DEV-03C：Temporal Worker工厂与Outbox轮询器。

Worker按队列拆分：Workflow Worker承载确定性`AgentTaskWorkflow`（Root）与
`AgentAttemptWorkflow`（Child），Control Worker承载PG事件游标/终态CAS与Attempt认领/
四重CAS Activity。五个队列按需扩缩，本批先起Workflow与Control两个。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from temporalio.client import Client
from temporalio.worker import Worker

from knowledge_system.modules.temporal.application.worker_versioning import (
    is_valid_build_id,
)
from knowledge_system.modules.temporal.domain.topology import (
    CONTROL_TASK_QUEUE,
    WORKFLOW_TASK_QUEUE,
)
from knowledge_system.modules.temporal.workflows.agent_attempt import AgentAttemptWorkflow
from knowledge_system.modules.temporal.workflows.agent_task import AgentTaskWorkflow
from knowledge_system.modules.temporal.workflows.attempt_activities import AttemptActivities
from knowledge_system.modules.temporal.workflows.control_activities import ControlActivities

logger = logging.getLogger(__name__)


class TemporalWorkerFactory:
    """由`temporalio.client.Client`构建本批两个Worker，注入已装配的Control Activity实例。

    Workflow Worker承载Root与Child两个确定性Workflow；Control Worker承载Root事件游标/
    终态CAS与Attempt认领/四重CAS。认知Activity（MODEL队列）本批无真实实现，不在此注册。
    """

    def __init__(
        self,
        client: Client,
        control_activities: ControlActivities,
        attempt_activities: AttemptActivities,
        *,
        workflow_task_queue: str = WORKFLOW_TASK_QUEUE,
        control_task_queue: str = CONTROL_TASK_QUEUE,
        build_id: str | None = None,
    ) -> None:
        if build_id is not None and not is_valid_build_id(build_id):
            raise ValueError("WORKER_BUILD_ID_INVALID")
        self._client = client
        self._control_activities = control_activities
        self._attempt_activities = attempt_activities
        self._workflow_task_queue = workflow_task_queue
        self._control_task_queue = control_task_queue
        self._build_id = build_id

    def build(self) -> list[Worker]:
        workflow_worker = Worker(
            self._client,
            task_queue=self._workflow_task_queue,
            workflows=[AgentTaskWorkflow, AgentAttemptWorkflow],
            build_id=self._build_id,
        )
        control_worker = Worker(
            self._client,
            task_queue=self._control_task_queue,
            activities=[
                self._control_activities.apply_business_events,
                self._control_activities.commit_root_terminal_state,
                self._attempt_activities.claim_next_attempt,
                self._attempt_activities.commit_attempt_terminal_state,
            ],
            build_id=self._build_id,
        )
        return [workflow_worker, control_worker]


class OutboxPollable(Protocol):
    """可被`OutboxPoller`循环驱动的单轮消费者端口。

    `poll_once`返回`claimed/published/retried/dead`四计数；`OutboxPublisher`与
    `OutboxSignalPublisher`各自返回具体结果类型，此处以`Any`吸收协程返回型的不变性。
    """

    async def poll_once(self) -> Any: ...


class OutboxPoller:
    """循环消费Outbox发件行；单轮失败不中断循环，`stop_event`/任务取消均能退出。

    `publisher`既可是`OutboxPublisher`（TEMPORAL_START），也可是`OutboxSignalPublisher`
    （TEMPORAL_SIGNAL）；两者`poll_once`均返回带`claimed`计数的结果，长循环逻辑完全相同。
    """

    def __init__(
        self, publisher: OutboxPollable, *, poll_interval_seconds: float = 1.0
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
                outcome = await self._publisher.poll_once()
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
