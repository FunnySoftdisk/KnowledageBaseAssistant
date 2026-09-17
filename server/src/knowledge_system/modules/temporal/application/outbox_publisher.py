"""DEV-03B：Outbox消费者——把`TEMPORAL_START`发件行启动为Root Workflow。

业务真值在PG：DEV-02发件载荷只携带`{task_id, event_id}`引用，本消费者从PG读取
`deadline_at`与`TASK_CREATED`事件`sequence`组装`AgentTaskWorkflowStartV1`，不信任载荷里
的陈旧快照。重复启动由`USE_EXISTING + REJECT_DUPLICATE`幂等吸收；本消费者对启动返回的
Run ID不落库（Root自会以`runtime_state_v1`与PG对账）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID, uuid4

from knowledge_system.infrastructure.persistence.outbox import (
    ClaimedOutboxMessage,
    OutboxStartContextMissingError,
    SqlAlchemyOutboxRepository,
)
from knowledge_system.modules.tasking.domain.core_types import canonical_json_sha256
from knowledge_system.modules.temporal.application.temporal_client import (
    TemporalWorkflowStarter,
)
from knowledge_system.modules.temporal.domain.contracts import (
    AgentTaskWorkflowArgsV1,
    AgentTaskWorkflowStartV1,
    RuntimeContractRefV1,
    TaskBudgetV1,
    TaskEventRefV1,
    TaskRefV1,
)
from knowledge_system.modules.temporal.domain.workflow_ids import (
    agent_task_workflow_id,
)

logger = logging.getLogger(__name__)

TEMPORAL_START_DESTINATION = "TEMPORAL_START"
START_SCHEMA_VERSION = "agent_task_workflow_start_v1"

# 错误码（写入outbox_message.last_error_code，≤64字符）。
OUTBOX_PAYLOAD_INVALID = "OUTBOX_PAYLOAD_INVALID"
OUTBOX_START_CONTEXT_MISSING = "OUTBOX_START_CONTEXT_MISSING"
TEMPORAL_START_FAILED = "TEMPORAL_START_FAILED"


@dataclass(frozen=True, slots=True)
class OutboxPollOutcome:
    """单轮认领的处置汇总，供轮询器与观测使用。"""

    claimed: int
    published: int
    retried: int
    dead: int


class OutboxPublisher:
    """认领→组装START→启动→写回状态；单轮`poll_once`，长循环交给`OutboxPoller`。"""

    def __init__(
        self,
        repository: SqlAlchemyOutboxRepository,
        starter: TemporalWorkflowStarter,
        *,
        batch_size: int = 32,
        lease_seconds: float = 60.0,
        max_attempts: int = 8,
    ) -> None:
        if batch_size < 1:
            raise ValueError("BATCH_SIZE_INVALID")
        if lease_seconds <= 0:
            raise ValueError("LEASE_SECONDS_INVALID")
        if max_attempts < 1:
            raise ValueError("MAX_ATTEMPTS_INVALID")
        self._repository = repository
        self._starter = starter
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts

    async def poll_once(self) -> OutboxPollOutcome:
        claim_id = f"temporal-start-{uuid4()}"
        messages = await self._repository.claim_batch(
            destination=TEMPORAL_START_DESTINATION,
            limit=self._batch_size,
            lease_seconds=self._lease_seconds,
            claim_id=claim_id,
        )
        published = 0
        retried = 0
        dead = 0
        for message in messages:
            disposition = await self._dispatch(message, claim_id)
            if disposition == "PUBLISHED":
                published += 1
            elif disposition == "RETRIED":
                retried += 1
            else:
                dead += 1
        return OutboxPollOutcome(
            claimed=len(messages), published=published, retried=retried, dead=dead
        )

    async def _dispatch(self, message: ClaimedOutboxMessage, claim_id: str) -> str:
        try:
            args = await self._build_start_args(message)
        except OutboxStartContextMissingError:
            await self._repository.mark_dead(message, claim_id, OUTBOX_START_CONTEXT_MISSING)
            return "DEAD"
        except (KeyError, ValueError, TypeError) as error:
            logger.warning("outbox payload invalid id=%s: %s", message.id, error)
            await self._repository.mark_dead(message, claim_id, OUTBOX_PAYLOAD_INVALID)
            return "DEAD"

        workflow_id = agent_task_workflow_id(args.root.task.task_id)
        try:
            await self._starter.start_task_workflow(workflow_id, args)
        except Exception as error:  # noqa: BLE001 —— 发布失败一律重试/置死
            logger.warning("temporal start failed id=%s: %s", message.id, error)
            publish_status = await self._repository.mark_retry(
                message,
                claim_id,
                TEMPORAL_START_FAILED,
                max_attempts=self._max_attempts,
            )
            return "DEAD" if publish_status == "DEAD" else "RETRIED"

        await self._repository.mark_published(message, claim_id)
        return "PUBLISHED"

    async def _build_start_args(self, message: ClaimedOutboxMessage) -> AgentTaskWorkflowArgsV1:
        if message.schema_version != START_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {message.schema_version}")
        if message.aggregate_type != "TASK" or message.aggregate_version != 1:
            raise ValueError("outbox aggregate shape mismatch")
        if canonical_json_sha256(message.payload_json) != message.payload_digest:
            raise ValueError("outbox payload digest mismatch")
        if set(message.payload_json) != {"task_id", "event_id"}:
            raise ValueError("outbox payload shape mismatch")
        task_id = UUID(str(message.payload_json["task_id"]))
        event_id = UUID(str(message.payload_json["event_id"]))
        if task_id != message.aggregate_id or event_id != message.event_id:
            raise ValueError("outbox payload identity mismatch")
        context = await self._repository.load_start_context(task_id, event_id)
        if context is None:
            raise OutboxStartContextMissingError(f"task/event missing: {task_id}")
        start = AgentTaskWorkflowStartV1(
            task=TaskRefV1(task_id=task_id),
            task_created_event=TaskEventRefV1(event_id=event_id, sequence=context.event_sequence),
            runtime_contract=RuntimeContractRefV1(),
            budget=TaskBudgetV1(wall_clock_deadline=context.deadline_at),
            run_sequence=1,
        )
        return AgentTaskWorkflowArgsV1(start)


__all__ = [
    "OUTBOX_PAYLOAD_INVALID",
    "OUTBOX_START_CONTEXT_MISSING",
    "OutboxPollOutcome",
    "OutboxPublisher",
    "TEMPORAL_START_FAILED",
]
