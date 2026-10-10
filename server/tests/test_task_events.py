"""DEV-07 应用层：Task 事件流重连快照（`TaskEventStreamService`）。

不接 FastAPI/SSE/Temporal，用假网关验证：终态短路、开放态按游标续读、授权投影过滤、
所有权隔离折叠 TASK_NOT_FOUND、游标/投影违规失败关闭。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4

from knowledge_system.modules.iam.domain.subject import Subject
from knowledge_system.modules.tasking.public import (
    SSEStreamError,
    TaskEntryError,
    TaskEventReconnectResultV1,
    TaskEventSnapshot,
    TaskEventStreamService,
    TaskRowSnapshot,
)

_TASK_ID = uuid4()


def make_subject(**overrides: object) -> Subject:
    base = dict(
        user_id=uuid4(),
        session_id=uuid4(),
        organization_id=uuid4(),
        credential_version=1,
    )
    base.update(overrides)
    return Subject(**base)


def make_row(**overrides: object) -> TaskRowSnapshot:
    base = dict(
        task_id=_TASK_ID,
        owner_id=uuid4(),
        organization_id=uuid4(),
        status="RUNNING",
        conversation_id=None,
        deadline_at=datetime.now(UTC),
        active_plan_version=None,
        error_code=None,
        result_artifact_id=None,
        current_input_id=uuid4(),
        version=1,
        last_event_sequence=0,
        rerun_of_task_id=None,
        created_at=datetime.now(UTC),
        started_at=None,
        completed_at=None,
        updated_at=datetime.now(UTC),
    )
    base.update(overrides)
    return TaskRowSnapshot(**base)


def make_event(sequence: int, event_type: str, **payload: object) -> TaskEventSnapshot:
    return TaskEventSnapshot(
        sequence=sequence,
        event_type=event_type,
        event_schema_version="task_event_v1",
        payload=dict(payload),
    )


class _FakeGateway:
    def __init__(
        self,
        row: TaskRowSnapshot | None,
        events: tuple[TaskEventSnapshot, ...] = (),
    ) -> None:
        self._row = row
        self._events = events
        self.list_events_calls: list[int] = []

    async def get_task(self, task_id: UUID) -> TaskRowSnapshot | None:
        return self._row

    async def list_events(
        self, task_id: UUID, after_sequence: int, limit: int
    ) -> tuple[TaskEventSnapshot, ...]:
        self.list_events_calls.append(after_sequence)
        return self._events


def reconnect(
    row: TaskRowSnapshot | None,
    subject: Subject,
    *,
    events: tuple[TaskEventSnapshot, ...] = (),
    last_event_id: str | None = None,
) -> tuple[TaskEventReconnectResultV1, _FakeGateway]:
    gateway = _FakeGateway(row, events)
    service = TaskEventStreamService(gateway)
    return asyncio.run(service.reconnect(_TASK_ID, subject, last_event_id)), gateway


class TerminalShortCircuitTest(unittest.TestCase):
    def test_completed_renders_success_without_reading_events(self) -> None:
        subject = make_subject()
        row = make_row(
            owner_id=subject.user_id,
            organization_id=subject.organization_id,
            status="COMPLETED",
            last_event_sequence=5,
        )
        result, gateway = reconnect(row, subject, events=(make_event(1, "TASK_CREATED"),))

        self.assertEqual(result.decision.action.value, "RENDER_RESULT")
        self.assertEqual(result.decision.terminal_display.value, "SUCCESS")
        self.assertEqual(result.events, ())
        self.assertEqual(gateway.list_events_calls, [])

    def test_failed_and_cancelled_never_success(self) -> None:
        subject = make_subject()
        for status, display in (("FAILED", "FAILED"), ("CANCELLED", "CANCELLED")):
            with self.subTest(status=status):
                row = make_row(
                    owner_id=subject.user_id,
                    organization_id=subject.organization_id,
                    status=status,
                    last_event_sequence=3,
                )
                result, _ = reconnect(row, subject)
                self.assertEqual(result.decision.action.value, "RENDER_RESULT")
                self.assertEqual(result.decision.terminal_display.value, display)
                self.assertNotEqual(result.decision.terminal_display.value, "SUCCESS")


class OpenResumeTest(unittest.TestCase):
    def test_fresh_cursor_projects_from_sequence_one(self) -> None:
        subject = make_subject()
        row = make_row(
            owner_id=subject.user_id,
            organization_id=subject.organization_id,
            status="RUNNING",
            last_event_sequence=2,
        )
        events = (
            make_event(1, "TASK_CREATED", status="QUEUED"),
            make_event(2, "PLAN_ACTIVATED"),
        )
        result, gateway = reconnect(row, subject, events=events)

        self.assertEqual(result.decision.action.value, "STREAM_RESUME")
        self.assertEqual(result.decision.resume_from_sequence, 1)
        self.assertEqual([e.sequence for e in result.events], [1, 2])
        self.assertEqual(gateway.list_events_calls, [0])

    def test_cursor_behind_resumes_after_last_seen(self) -> None:
        subject = make_subject()
        row = make_row(
            owner_id=subject.user_id,
            organization_id=subject.organization_id,
            status="RUNNING",
            last_event_sequence=5,
        )
        events = (make_event(4, "PLAN_ACTIVATED"), make_event(5, "USER_INPUT_REQUESTED"))
        result, gateway = reconnect(row, subject, events=events, last_event_id="3")

        self.assertEqual(result.decision.resume_from_sequence, 4)
        self.assertEqual([e.sequence for e in result.events], [4, 5])
        self.assertEqual(gateway.list_events_calls, [3])

    def test_caught_up_marks_heartbeat_required(self) -> None:
        subject = make_subject()
        row = make_row(
            owner_id=subject.user_id,
            organization_id=subject.organization_id,
            status="RUNNING",
            last_event_sequence=5,
        )
        result, _ = reconnect(row, subject, events=(), last_event_id="5")

        self.assertTrue(result.decision.heartbeat_required)
        self.assertEqual(result.events, ())


class ProjectionFilterTest(unittest.TestCase):
    def test_non_streamable_events_are_filtered_out(self) -> None:
        subject = make_subject()
        row = make_row(
            owner_id=subject.user_id,
            organization_id=subject.organization_id,
            status="RUNNING",
            last_event_sequence=2,
        )
        events = (
            make_event(1, "TASK_CREATED", status="QUEUED"),
            make_event(2, "OBSERVATION_SET_COMMITTED"),
        )
        result, _ = reconnect(row, subject, events=events)

        self.assertEqual([e.sequence for e in result.events], [1])
        self.assertEqual(result.events[0].event_type, "TASK_CREATED")


class FailureCloseTest(unittest.TestCase):
    def test_forbidden_payload_propagates(self) -> None:
        subject = make_subject()
        row = make_row(
            owner_id=subject.user_id,
            organization_id=subject.organization_id,
            status="RUNNING",
            last_event_sequence=1,
        )
        gateway = _FakeGateway(row, (make_event(1, "TASK_CREATED", api_key="secret"),))
        service = TaskEventStreamService(gateway)
        with self.assertRaises(SSEStreamError) as ctx:
            asyncio.run(service.reconnect(_TASK_ID, subject, None))
        self.assertEqual(ctx.exception.code, "SSE_FORBIDDEN_PAYLOAD_FIELD")

    def test_malformed_cursor_is_rejected(self) -> None:
        subject = make_subject()
        row = make_row(
            owner_id=subject.user_id,
            organization_id=subject.organization_id,
            status="RUNNING",
            last_event_sequence=1,
        )
        gateway = _FakeGateway(row, ())
        service = TaskEventStreamService(gateway)
        with self.assertRaises(SSEStreamError) as ctx:
            asyncio.run(service.reconnect(_TASK_ID, subject, "not-a-number"))
        self.assertEqual(ctx.exception.code, "SSE_CURSOR_INVALID")

    def test_cursor_ahead_of_server_is_rejected(self) -> None:
        subject = make_subject()
        row = make_row(
            owner_id=subject.user_id,
            organization_id=subject.organization_id,
            status="RUNNING",
            last_event_sequence=3,
        )
        gateway = _FakeGateway(row, ())
        service = TaskEventStreamService(gateway)
        with self.assertRaises(SSEStreamError) as ctx:
            asyncio.run(service.reconnect(_TASK_ID, subject, "5"))
        self.assertEqual(ctx.exception.code, "SSE_CURSOR_AHEAD_OF_SERVER")


class OwnershipIsolationTest(unittest.TestCase):
    def test_cross_owner_folds_to_task_not_found(self) -> None:
        subject = make_subject()
        row = make_row(owner_id=uuid4(), organization_id=uuid4())
        gateway = _FakeGateway(row, ())
        service = TaskEventStreamService(gateway)
        with self.assertRaises(TaskEntryError) as ctx:
            asyncio.run(service.reconnect(_TASK_ID, subject, None))
        self.assertEqual(ctx.exception.code, "TASK_NOT_FOUND")

    def test_missing_task_folds_to_task_not_found(self) -> None:
        subject = make_subject()
        gateway = _FakeGateway(None, ())
        service = TaskEventStreamService(gateway)
        with self.assertRaises(TaskEntryError) as ctx:
            asyncio.run(service.reconnect(_TASK_ID, subject, None))
        self.assertEqual(ctx.exception.code, "TASK_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
