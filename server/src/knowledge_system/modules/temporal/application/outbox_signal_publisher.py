"""DEV-07：Outbox消费者——把`TEMPORAL_SIGNAL`发件行投递为Root Workflow业务事件信号。

取消端点在同一事务内提交`TASK_CANCELLED`事件与`TEMPORAL_SIGNAL`发件行后，本消费者认领
该行并投递`business_event_available_v1`无参信号；Workflow被唤醒后经control Activity按PG
游标重读已提交事件，取消状态由此推进到`CANCELLED`终态。信号不携带业务载荷（Workflow只信任
PG），投递至少一次：重复信号只是布尔唤醒 + 按序游标重读，幂等安全。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID, uuid4

from knowledge_system.infrastructure.persistence.outbox import (
    ClaimedOutboxMessage,
    SqlAlchemyOutboxRepository,
)
from knowledge_system.modules.tasking.domain.core_types import canonical_json_sha256
from knowledge_system.modules.temporal.application.temporal_client import (
    TemporalWorkflowSignaller,
)
from knowledge_system.modules.temporal.domain.workflow_ids import (
    BUSINESS_EVENT_SIGNAL,
    agent_task_workflow_id,
)

logger = logging.getLogger(__name__)

TEMPORAL_SIGNAL_DESTINATION = "TEMPORAL_SIGNAL"
SIGNAL_SCHEMA_VERSION = "agent_task_workflow_signal_v1"

# 错误码（写入outbox_message.last_error_code，≤64字符）。
OUTBOX_SIGNAL_PAYLOAD_INVALID = "OUTBOX_SIGNAL_PAYLOAD_INVALID"
TEMPORAL_SIGNAL_FAILED = "TEMPORAL_SIGNAL_FAILED"


@dataclass(frozen=True, slots=True)
class OutboxSignalPollOutcome:
    """单轮认领的处置汇总，供轮询器与观测使用。"""

    claimed: int
    published: int
    retried: int
    dead: int


class OutboxSignalPublisher:
    """认领→校验信号目标→投递信号→写回状态；单轮`poll_once`，长循环交给`OutboxPoller`。"""

    def __init__(
        self,
        repository: SqlAlchemyOutboxRepository,
        signaller: TemporalWorkflowSignaller,
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
        self._signaller = signaller
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts

    async def poll_once(self) -> OutboxSignalPollOutcome:
        claim_id = f"temporal-signal-{uuid4()}"
        messages = await self._repository.claim_batch(
            destination=TEMPORAL_SIGNAL_DESTINATION,
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
        return OutboxSignalPollOutcome(
            claimed=len(messages), published=published, retried=retried, dead=dead
        )

    async def _dispatch(self, message: ClaimedOutboxMessage, claim_id: str) -> str:
        try:
            workflow_id = self._build_workflow_id(message)
        except (KeyError, ValueError, TypeError) as error:
            logger.warning("outbox signal payload invalid id=%s: %s", message.id, error)
            await self._repository.mark_dead(message, claim_id, OUTBOX_SIGNAL_PAYLOAD_INVALID)
            return "DEAD"

        try:
            await self._signaller.signal_task_workflow(workflow_id, BUSINESS_EVENT_SIGNAL)
        except Exception as error:  # noqa: BLE001 —— 投递失败一律重试/置死
            logger.warning("temporal signal failed id=%s: %s", message.id, error)
            publish_status = await self._repository.mark_retry(
                message,
                claim_id,
                TEMPORAL_SIGNAL_FAILED,
                max_attempts=self._max_attempts,
            )
            return "DEAD" if publish_status == "DEAD" else "RETRIED"

        await self._repository.mark_published(message, claim_id)
        return "PUBLISHED"

    def _build_workflow_id(self, message: ClaimedOutboxMessage) -> str:
        """校验信号发件行并解析Root Workflow ID；失败关闭抛`ValueError`。"""

        if message.schema_version != SIGNAL_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {message.schema_version}")
        if message.aggregate_type != "TASK":
            raise ValueError("outbox aggregate shape mismatch")
        if canonical_json_sha256(message.payload_json) != message.payload_digest:
            raise ValueError("outbox payload digest mismatch")
        if set(message.payload_json) != {"task_id", "event_id"}:
            raise ValueError("outbox payload shape mismatch")
        task_id = UUID(str(message.payload_json["task_id"]))
        event_id = UUID(str(message.payload_json["event_id"]))
        if task_id != message.aggregate_id or event_id != message.event_id:
            raise ValueError("outbox payload identity mismatch")
        return agent_task_workflow_id(task_id)


__all__ = [
    "OUTBOX_SIGNAL_PAYLOAD_INVALID",
    "OutboxSignalPollOutcome",
    "OutboxSignalPublisher",
    "SIGNAL_SCHEMA_VERSION",
    "TEMPORAL_SIGNAL_DESTINATION",
    "TEMPORAL_SIGNAL_FAILED",
]
