"""DEV-07 TEMPORAL_SIGNAL Outbox消费者的离线契约：信号目标解析与投递处置。

不依赖运行中的 Temporal/PostgreSQL，以 Fake 仓储 + Fake 投递器验证：
- 合法信号行解析为 `agent-task-v1-{task_id}` 并投递 `business_event_available_v1`；
- 投递失败在预算内回到 PENDING、超次置 DEAD；
- 载荷损坏（schema/聚合类型/形状/摘要/身份不符）一律失败关闭置 DEAD。
"""

from __future__ import annotations

import unittest
from uuid import UUID, uuid4

from knowledge_system.infrastructure.persistence.outbox import ClaimedOutboxMessage
from knowledge_system.modules.tasking.domain.core_types import canonical_json_sha256
from knowledge_system.modules.temporal.application.outbox_signal_publisher import (
    OUTBOX_SIGNAL_PAYLOAD_INVALID,
    OutboxSignalPublisher,
)

SIGNAL_SCHEMA = "agent_task_workflow_signal_v1"


class FakeSignaller:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def signal_task_workflow(self, workflow_id: str, signal_name: str) -> None:
        if self.fail:
            raise RuntimeError("TEMPORAL_SIGNAL_UNAVAILABLE")
        self.calls.append((workflow_id, signal_name))


class FakeOutboxRepository:
    def __init__(self, messages: list[ClaimedOutboxMessage]) -> None:
        self._messages = list(messages)
        self.published: list[UUID] = []
        self.dead: list[tuple[UUID, str]] = []
        self.retried: list[tuple[UUID, str]] = []

    async def claim_batch(
        self, *, destination: str, limit: int, lease_seconds: float, claim_id: str
    ):
        assert destination == "TEMPORAL_SIGNAL"
        return self._messages

    async def mark_published(self, message: ClaimedOutboxMessage, claim_id: str) -> None:
        self.published.append(message.id)

    async def mark_dead(
        self, message: ClaimedOutboxMessage, claim_id: str, error_code: str
    ) -> None:
        self.dead.append((message.id, error_code))

    async def mark_retry(
        self,
        message: ClaimedOutboxMessage,
        claim_id: str,
        error_code: str,
        *,
        max_attempts: int,
    ) -> str:
        if message.attempt_count >= max_attempts:
            self.dead.append((message.id, error_code))
            return "DEAD"
        self.retried.append((message.id, error_code))
        return "PENDING"


def _base_message(task_id: UUID, event_id: UUID, *, attempt_count: int = 1) -> dict:
    payload = {"task_id": str(task_id), "event_id": str(event_id)}
    return {
        "id": uuid4(),
        "event_id": event_id,
        "aggregate_type": "TASK",
        "aggregate_id": task_id,
        "aggregate_version": 2,
        "schema_version": SIGNAL_SCHEMA,
        "payload_json": payload,
        "payload_digest": canonical_json_sha256(payload),
        "attempt_count": attempt_count,
    }


class OutboxSignalPublisherTests(unittest.IsolatedAsyncioTestCase):
    def _publisher(
        self,
        messages: list[ClaimedOutboxMessage],
        *,
        fail: bool = False,
        max_attempts: int = 8,
    ):
        repository = FakeOutboxRepository(messages)
        signaller = FakeSignaller(fail=fail)
        publisher = OutboxSignalPublisher(
            repository,  # type: ignore[arg-type] —— 离线替身满足同一方法签名
            signaller,  # type: ignore[arg-type] —— 离线替身满足同一方法签名
            max_attempts=max_attempts,
        )
        return publisher, repository, signaller

    async def test_valid_signal_resolves_workflow_id_and_publishes(self) -> None:
        task_id = uuid4()
        event_id = uuid4()
        message = ClaimedOutboxMessage(**_base_message(task_id, event_id))
        publisher, repository, signaller = self._publisher([message])

        outcome = await publisher.poll_once()

        self.assertEqual(outcome.claimed, 1)
        self.assertEqual(outcome.published, 1)
        self.assertEqual(
            signaller.calls,
            [(f"agent-task-v1-{task_id}", "business_event_available_v1")],
        )
        self.assertEqual(repository.published, [message.id])
        self.assertEqual(repository.retried, [])
        self.assertEqual(repository.dead, [])

    async def test_signal_failure_within_budget_returns_to_pending(self) -> None:
        task_id = uuid4()
        message = ClaimedOutboxMessage(**_base_message(task_id, uuid4(), attempt_count=1))
        publisher, repository, _ = self._publisher([message], fail=True, max_attempts=8)

        outcome = await publisher.poll_once()

        self.assertEqual(outcome.retried, 1)
        self.assertEqual(outcome.dead, 0)
        self.assertEqual(len(repository.retried), 1)
        self.assertEqual(repository.retried[0][1], "TEMPORAL_SIGNAL_FAILED")

    async def test_signal_failure_at_max_attempts_marks_dead(self) -> None:
        task_id = uuid4()
        message = ClaimedOutboxMessage(**_base_message(task_id, uuid4(), attempt_count=8))
        publisher, repository, _ = self._publisher([message], fail=True, max_attempts=8)

        outcome = await publisher.poll_once()

        self.assertEqual(outcome.dead, 1)
        self.assertEqual(len(repository.dead), 1)
        self.assertEqual(repository.dead[0][1], "TEMPORAL_SIGNAL_FAILED")

    async def test_unsupported_schema_marks_dead(self) -> None:
        task_id = uuid4()
        message = ClaimedOutboxMessage(
            **{**_base_message(task_id, uuid4()), "schema_version": "agent_task_workflow_start_v1"}
        )
        publisher, repository, _ = self._publisher([message])

        outcome = await publisher.poll_once()

        self.assertEqual(outcome.dead, 1)
        self.assertEqual(repository.dead[0][1], OUTBOX_SIGNAL_PAYLOAD_INVALID)

    async def test_non_task_aggregate_marks_dead(self) -> None:
        task_id = uuid4()
        message = ClaimedOutboxMessage(
            **{**_base_message(task_id, uuid4()), "aggregate_type": "ATTEMPT"}
        )
        publisher, repository, _ = self._publisher([message])

        outcome = await publisher.poll_once()

        self.assertEqual(outcome.dead, 1)
        self.assertEqual(repository.dead[0][1], OUTBOX_SIGNAL_PAYLOAD_INVALID)

    async def test_payload_shape_mismatch_marks_dead(self) -> None:
        task_id = uuid4()
        payload = {"task_id": str(task_id)}
        message = ClaimedOutboxMessage(
            **{
                **_base_message(task_id, uuid4()),
                "payload_json": payload,
                "payload_digest": canonical_json_sha256(payload),
            }
        )
        publisher, repository, _ = self._publisher([message])

        outcome = await publisher.poll_once()

        self.assertEqual(outcome.dead, 1)
        self.assertEqual(repository.dead[0][1], OUTBOX_SIGNAL_PAYLOAD_INVALID)

    async def test_payload_digest_mismatch_marks_dead(self) -> None:
        task_id = uuid4()
        event_id = uuid4()
        base = _base_message(task_id, event_id)
        message = ClaimedOutboxMessage(
            **{**base, "payload_json": {"task_id": str(uuid4()), "event_id": str(event_id)}}
        )
        publisher, repository, _ = self._publisher([message])

        outcome = await publisher.poll_once()

        self.assertEqual(outcome.dead, 1)
        self.assertEqual(repository.dead[0][1], OUTBOX_SIGNAL_PAYLOAD_INVALID)

    async def test_payload_identity_mismatch_marks_dead(self) -> None:
        task_id = uuid4()
        event_id = uuid4()
        other_task = uuid4()
        payload = {"task_id": str(other_task), "event_id": str(event_id)}
        message = ClaimedOutboxMessage(
            **{
                **_base_message(task_id, event_id),
                "payload_json": payload,
                "payload_digest": canonical_json_sha256(payload),
            }
        )
        publisher, repository, _ = self._publisher([message])

        outcome = await publisher.poll_once()

        self.assertEqual(outcome.dead, 1)
        self.assertEqual(repository.dead[0][1], OUTBOX_SIGNAL_PAYLOAD_INVALID)


if __name__ == "__main__":
    unittest.main()
