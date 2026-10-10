"""DEV-07 API：`GET /api/v1/tasks/{task_id}/events` SSE 续读端点。

用 TestClient + 假网关验证：终态短路发 `terminal` 帧、开放态按游标续读发 `id/event/data`、
追平发心跳注释与 `retry`、跨主体 404、非法游标 400、游标越界 409。
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from knowledge_system.api.app import create_app
from knowledge_system.api.dependencies import get_subject, get_task_event_stream_service
from knowledge_system.api.settings import SecretRef, Settings
from knowledge_system.modules.iam.public import Subject
from knowledge_system.modules.tasking.public import (
    TaskEventSnapshot,
    TaskEventStreamService,
    TaskRowSnapshot,
)

os.environ.setdefault("TEST_JWT_SIGNING_SECRET", "j" * 64)
os.environ.setdefault("TEST_IDEMPOTENCY_HMAC_SECRET", "h" * 64)


def make_settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://u:p@localhost:5432/db",
        jwt_signing_secret_ref=SecretRef(kind="env", reference="TEST_JWT_SIGNING_SECRET"),
        idempotency_hmac_secret_ref=SecretRef(kind="env", reference="TEST_IDEMPOTENCY_HMAC_SECRET"),
    )


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
        task_id=uuid4(),
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

    async def get_task(self, task_id: UUID) -> TaskRowSnapshot | None:
        return self._row

    async def list_events(
        self, task_id: UUID, after_sequence: int, limit: int
    ) -> tuple[TaskEventSnapshot, ...]:
        return self._events


class TaskEventStreamRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(make_settings())
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()

    def _install(
        self,
        subject: Subject,
        row: TaskRowSnapshot | None,
        events: tuple[TaskEventSnapshot, ...] = (),
    ) -> None:
        service = TaskEventStreamService(_FakeGateway(row, events))
        self.app.dependency_overrides[get_subject] = lambda: subject
        self.app.dependency_overrides[get_task_event_stream_service] = lambda: service

    def test_terminal_emits_terminal_frame(self) -> None:
        subject = make_subject()
        row = make_row(
            owner_id=subject.user_id,
            organization_id=subject.organization_id,
            status="COMPLETED",
            last_event_sequence=1,
        )
        self._install(subject, row)

        resp = self.client.get(f"/api/v1/tasks/{row.task_id}/events")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["content-type"].startswith("text/event-stream"))
        self.assertIn("event: terminal", resp.text)
        self.assertIn('"display":"SUCCESS"', resp.text)

    def test_open_emits_projected_events_and_retry(self) -> None:
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
        self._install(subject, row, events=events)

        resp = self.client.get(f"/api/v1/tasks/{row.task_id}/events")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("id: 1", resp.text)
        self.assertIn("event: TASK_CREATED", resp.text)
        self.assertIn('"status":"QUEUED"', resp.text)
        self.assertIn("id: 2", resp.text)
        self.assertIn("retry: 1000", resp.text)

    def test_caught_up_emits_heartbeat_comment(self) -> None:
        subject = make_subject()
        row = make_row(
            owner_id=subject.user_id,
            organization_id=subject.organization_id,
            status="RUNNING",
            last_event_sequence=3,
        )
        self._install(subject, row, events=())

        resp = self.client.get(
            f"/api/v1/tasks/{row.task_id}/events",
            headers={"Last-Event-ID": "3"},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIn(": ping", resp.text)
        self.assertIn("retry: 1000", resp.text)

    def test_cross_owner_is_404(self) -> None:
        subject = make_subject()
        row = make_row(owner_id=uuid4(), organization_id=uuid4())
        self._install(subject, row)

        resp = self.client.get(f"/api/v1/tasks/{row.task_id}/events")

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "TASK_NOT_FOUND")

    def test_malformed_cursor_is_400(self) -> None:
        subject = make_subject()
        row = make_row(
            owner_id=subject.user_id,
            organization_id=subject.organization_id,
            status="RUNNING",
            last_event_sequence=1,
        )
        self._install(subject, row)

        resp = self.client.get(
            f"/api/v1/tasks/{row.task_id}/events",
            headers={"Last-Event-ID": "abc"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "SSE_CURSOR_INVALID")

    def test_cursor_ahead_of_server_is_409(self) -> None:
        subject = make_subject()
        row = make_row(
            owner_id=subject.user_id,
            organization_id=subject.organization_id,
            status="RUNNING",
            last_event_sequence=2,
        )
        self._install(subject, row)

        resp = self.client.get(
            f"/api/v1/tasks/{row.task_id}/events",
            headers={"Last-Event-ID": "5"},
        )

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "SSE_CURSOR_AHEAD_OF_SERVER")


if __name__ == "__main__":
    unittest.main()
