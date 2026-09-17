"""DEV-02C API任务路由：创建（幂等）、详情、结果与统一错误信封。"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from knowledge_system.api.app import create_app
from knowledge_system.api.dependencies import (
    get_subject,
    get_task_creation_service,
    get_task_query_service,
)
from knowledge_system.api.settings import SecretRef, Settings
from knowledge_system.infrastructure.persistence import (
    TaskCreationDisposition,
    TaskCreationTransactionResult,
)
from knowledge_system.modules.iam.public import Subject
from knowledge_system.modules.tasking.public import (
    ConversationEntrySnapshot,
    TaskCreationService,
    TaskQueryService,
    TaskRowSnapshot,
)

os.environ.setdefault("TEST_JWT_SIGNING_SECRET", "j" * 64)
os.environ.setdefault("TEST_IDEMPOTENCY_HMAC_SECRET", "h" * 64)

SECRET = b"s" * 32


def make_settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://u:p@localhost:5432/db",
        jwt_signing_secret_ref=SecretRef(kind="env", reference="TEST_JWT_SIGNING_SECRET"),
        idempotency_hmac_secret_ref=SecretRef(
            kind="env", reference="TEST_IDEMPOTENCY_HMAC_SECRET"
        ),
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


def make_conversation(organization_id: UUID) -> ConversationEntrySnapshot:
    return ConversationEntrySnapshot(
        conversation_id=uuid4(),
        organization_id=organization_id,
        next_message_sequence=1,
    )


def _create_body(conversation_id: UUID, query: str = "hello") -> dict[str, object]:
    return {
        "conversation_id": str(conversation_id),
        "query": query,
        "attachments": [],
        "knowledge_scope": None,
        "output_contract": None,
        "network_policy": None,
        "selected_resource_refs": [],
        "explicit_constraints": [],
    }


def make_row(**overrides: object) -> TaskRowSnapshot:
    base = dict(
        task_id=uuid4(),
        owner_id=uuid4(),
        organization_id=uuid4(),
        status="QUEUED",
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


class _FakeTaskEntryGateway:
    def __init__(self, conversation: ConversationEntrySnapshot | None) -> None:
        self._conversation = conversation

    async def load_conversation(
        self, conversation_id: UUID, owner_id: UUID
    ) -> ConversationEntrySnapshot | None:
        return self._conversation

    async def load_attachment(self, artifact_id: UUID, owner_id: UUID) -> None:
        return None


class _FakeTransactionService:
    def __init__(
        self,
        disposition: TaskCreationDisposition = TaskCreationDisposition.CREATED,
        committed_task_id: UUID | None = None,
    ) -> None:
        self._disposition = disposition
        self._committed_task_id = committed_task_id
        self.write_set: object | None = None

    async def execute(self, write_set: object) -> TaskCreationTransactionResult:
        self.write_set = write_set
        task_id = self._committed_task_id or write_set.task.id
        input_id = write_set.initial_input.input_id
        return TaskCreationTransactionResult(
            disposition=self._disposition,
            task_id=task_id,
            response_code=202,
            response_body={
                "task_id": str(task_id),
                "input_id": str(input_id),
                "status": "QUEUED",
                "deadline_at": "2026-01-01T00:00:00+00:00",
                "active_plan_version": None,
            },
            response_digest="d" * 64,
        )


class _FakeTaskQueryGateway:
    def __init__(self, row: TaskRowSnapshot | None) -> None:
        self._row = row

    async def get_task(self, task_id: UUID) -> TaskRowSnapshot | None:
        return self._row


class TaskCreateRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(make_settings())
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()

    def _install(
        self,
        subject: Subject,
        conversation: ConversationEntrySnapshot | None,
        *,
        transaction: _FakeTransactionService | None = None,
    ) -> _FakeTransactionService:
        tx = transaction or _FakeTransactionService()
        service = TaskCreationService(tx, _FakeTaskEntryGateway(conversation), SECRET)
        self.app.dependency_overrides[get_subject] = lambda: subject
        self.app.dependency_overrides[get_task_creation_service] = lambda: service
        return tx

    def test_create_returns_202_with_queued_envelope(self) -> None:
        subject = make_subject()
        conversation = make_conversation(subject.organization_id)
        tx = self._install(subject, conversation)

        resp = self.client.post(
            "/api/v1/tasks",
            json=_create_body(conversation.conversation_id),
            headers={"Idempotency-Key": str(uuid4())},
        )

        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(body["status"], "QUEUED")
        self.assertEqual(body["task_id"], str(tx.write_set.task.id))
        self.assertEqual(body["input_id"], str(tx.write_set.initial_input.input_id))
        self.assertIsNone(body["active_plan_version"])
        self.assertTrue(resp.headers["X-Request-Id"])
        self.assertTrue(resp.headers["X-Trace-Id"])
        self.assertEqual(
            tx.write_set.audit_event.trace_id,
            resp.headers["X-Trace-Id"],
        )

    def test_create_replay_returns_committed_task_id(self) -> None:
        subject = make_subject()
        conversation = make_conversation(subject.organization_id)
        committed = uuid4()
        self._install(
            subject,
            conversation,
            transaction=_FakeTransactionService(
                disposition=TaskCreationDisposition.REPLAYED, committed_task_id=committed
            ),
        )

        resp = self.client.post(
            "/api/v1/tasks",
            json=_create_body(conversation.conversation_id),
            headers={"Idempotency-Key": str(uuid4())},
        )

        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json()["task_id"], str(committed))

    def test_create_missing_idempotency_key_is_422(self) -> None:
        subject = make_subject()
        conversation = make_conversation(subject.organization_id)
        self._install(subject, conversation)

        resp = self.client.post(
            "/api/v1/tasks",
            json=_create_body(conversation.conversation_id),
        )

        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["code"], "IDEMPOTENCY_KEY_INVALID")

    def test_create_non_uuid_idempotency_key_is_422(self) -> None:
        subject = make_subject()
        conversation = make_conversation(subject.organization_id)
        self._install(subject, conversation)

        resp = self.client.post(
            "/api/v1/tasks",
            json=_create_body(conversation.conversation_id),
            headers={"Idempotency-Key": "not-a-uuid"},
        )

        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["code"], "IDEMPOTENCY_KEY_INVALID")

    def test_create_body_too_large_is_413(self) -> None:
        subject = make_subject()
        conversation = make_conversation(subject.organization_id)
        self._install(subject, conversation)

        resp = self.client.post(
            "/api/v1/tasks",
            json={
                "conversation_id": str(conversation.conversation_id),
                "query": "x" * 300_000,
            },
            headers={"Idempotency-Key": str(uuid4())},
        )

        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.json()["code"], "CREATE_TASK_BODY_SIZE_LIMIT")

    def test_create_duplicate_json_key_is_422(self) -> None:
        subject = make_subject()
        conversation = make_conversation(subject.organization_id)
        self._install(subject, conversation)
        raw = (
            f'{{"conversation_id": "{conversation.conversation_id}", "query": "a", "query": "b"}}'
        )

        resp = self.client.post(
            "/api/v1/tasks",
            content=raw,
            headers={"Idempotency-Key": str(uuid4()), "Content-Type": "application/json"},
        )

        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["code"], "DUPLICATE_JSON_KEY")

    def test_create_missing_query_returns_field_errors(self) -> None:
        subject = make_subject()
        conversation = make_conversation(subject.organization_id)
        self._install(subject, conversation)

        body = _create_body(conversation.conversation_id)
        del body["query"]
        resp = self.client.post(
            "/api/v1/tasks",
            json=body,
            headers={"Idempotency-Key": str(uuid4())},
        )

        self.assertEqual(resp.status_code, 422)
        body = resp.json()
        self.assertEqual(body["code"], "REQUEST_VALIDATION_FAILED")
        self.assertTrue(any(fe["field"] == "query" for fe in body["field_errors"]))

    def test_create_cross_org_conversation_is_404(self) -> None:
        subject = make_subject()
        conversation = make_conversation(uuid4())
        self._install(subject, conversation)

        resp = self.client.post(
            "/api/v1/tasks",
            json=_create_body(conversation.conversation_id),
            headers={"Idempotency-Key": str(uuid4())},
        )

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "CONVERSATION_NOT_FOUND")


class TaskQueryRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(make_settings())
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()

    def _install(self, subject: Subject, row: TaskRowSnapshot | None) -> None:
        service = TaskQueryService(_FakeTaskQueryGateway(row))
        self.app.dependency_overrides[get_subject] = lambda: subject
        self.app.dependency_overrides[get_task_query_service] = lambda: service

    def test_get_detail_returns_owned_task(self) -> None:
        subject = make_subject()
        row = make_row(owner_id=subject.user_id, organization_id=subject.organization_id)
        self._install(subject, row)

        resp = self.client.get(f"/api/v1/tasks/{row.task_id}")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["task_id"], str(row.task_id))
        self.assertEqual(body["status"], "QUEUED")
        self.assertEqual(body["version"], row.version)

    def test_get_detail_cross_owner_is_404(self) -> None:
        subject = make_subject()
        row = make_row(owner_id=uuid4())
        self._install(subject, row)

        resp = self.client.get(f"/api/v1/tasks/{row.task_id}")

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "TASK_NOT_FOUND")

    def test_get_detail_missing_is_404(self) -> None:
        subject = make_subject()
        self._install(subject, None)

        resp = self.client.get(f"/api/v1/tasks/{uuid4()}")

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "TASK_NOT_FOUND")

    def test_get_result_returns_result_view(self) -> None:
        subject = make_subject()
        result_artifact_id = uuid4()
        row = make_row(
            owner_id=subject.user_id,
            organization_id=subject.organization_id,
            status="COMPLETED",
            result_artifact_id=result_artifact_id,
            completed_at=datetime.now(UTC),
        )
        self._install(subject, row)

        resp = self.client.get(f"/api/v1/tasks/{row.task_id}/result")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["task_id"], str(row.task_id))
        self.assertEqual(body["status"], "COMPLETED")
        self.assertEqual(body["result_artifact_id"], str(result_artifact_id))


if __name__ == "__main__":
    unittest.main()
