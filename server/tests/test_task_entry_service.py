"""DEV-02B任务入口应用服务：创建编排映射与所有者隔离查询。"""

from __future__ import annotations

import hashlib
import hmac
import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4

from knowledge_system.infrastructure.persistence import (
    TaskCreationDisposition,
    TaskCreationTransactionResult,
)
from knowledge_system.modules.iam.domain.subject import Subject
from knowledge_system.modules.tasking.application.errors import TaskEntryError
from knowledge_system.modules.tasking.application.gateway import (
    AttachmentEntrySnapshot,
    ConversationEntrySnapshot,
    TaskRowSnapshot,
)
from knowledge_system.modules.tasking.application.task_creation import (
    TaskCreationService,
)
from knowledge_system.modules.tasking.application.task_queries import TaskQueryService
from knowledge_system.modules.tasking.domain.core_types import SchemaRefV1
from knowledge_system.modules.tasking.domain.input_contracts import (
    CreateTaskAttachmentV1,
    CreateTaskRequestV1,
    OutputContractV1,
)

SECRET = b"s" * 32


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


def make_attachment(organization_id: UUID, **overrides: object) -> AttachmentEntrySnapshot:
    base = dict(
        artifact_id=uuid4(),
        organization_id=organization_id,
        artifact_type="DOCUMENT",
        sha256="a" * 64,
        schema_id="doc",
        schema_version="v1",
        detected_media_type="application/pdf",
        purpose="TASK_ATTACHMENT",
        state="AVAILABLE",
    )
    base.update(overrides)
    return AttachmentEntrySnapshot(**base)


def make_request(conversation_id: UUID, **overrides: object) -> CreateTaskRequestV1:
    base = dict(
        conversation_id=conversation_id,
        query="hello",
        attachments=(),
        knowledge_scope=None,
        output_contract=None,
        network_policy=None,
        selected_resource_refs=(),
        explicit_constraints=(),
    )
    base.update(overrides)
    return CreateTaskRequestV1(**base)


class FakeTaskEntryGateway:
    def __init__(
        self,
        conversation: ConversationEntrySnapshot | None,
        attachments: dict[UUID, AttachmentEntrySnapshot] | None = None,
    ) -> None:
        self._conversation = conversation
        self._attachments = attachments or {}

    async def load_conversation(
        self, conversation_id: UUID, owner_id: UUID
    ) -> ConversationEntrySnapshot | None:
        return self._conversation

    async def load_attachment(
        self, artifact_id: UUID, owner_id: UUID
    ) -> AttachmentEntrySnapshot | None:
        return self._attachments.get(artifact_id)


class FakeTransactionService:
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
        task_id = self._committed_task_id or write_set.task.id  # type: ignore[attr-defined]
        input_id = write_set.initial_input.input_id  # type: ignore[attr-defined]
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


class FakeTaskQueryGateway:
    def __init__(self, row: TaskRowSnapshot | None) -> None:
        self._row = row

    async def get_task(self, task_id: UUID) -> TaskRowSnapshot | None:
        return self._row


class TaskCreationServiceTests(unittest.IsolatedAsyncioTestCase):
    def _service(
        self,
        subject: Subject,
        conversation: ConversationEntrySnapshot | None,
        attachments: dict[UUID, AttachmentEntrySnapshot] | None = None,
        *,
        committed_task_id: UUID | None = None,
        disposition: TaskCreationDisposition = TaskCreationDisposition.CREATED,
    ) -> tuple[TaskCreationService, FakeTransactionService]:
        gateway = FakeTaskEntryGateway(conversation, attachments)
        transaction = FakeTransactionService(disposition, committed_task_id)
        return TaskCreationService(transaction, gateway, SECRET), transaction

    async def test_create_maps_to_queued_write_set(self) -> None:
        subject = make_subject()
        conversation = make_conversation(subject.organization_id)
        service, transaction = self._service(subject, conversation)

        result = await service.create(
            make_request(conversation.conversation_id), subject, idempotency_key="key-1"
        )

        self.assertEqual(result.disposition, "CREATED")
        write_set = transaction.write_set  # type: ignore[attr-defined]
        self.assertEqual(result.task_id, write_set.task.id)
        self.assertEqual(write_set.task.status, "QUEUED")
        self.assertEqual(write_set.task.organization_id, subject.organization_id)
        self.assertEqual(write_set.task.owner_id, subject.user_id)
        self.assertEqual(write_set.query_message.role, "USER")
        self.assertEqual(write_set.query_message.message_kind, "USER_QUERY")
        self.assertEqual(write_set.query_message.sequence, conversation.next_message_sequence)
        self.assertIsNotNone(write_set.query_artifact)
        self.assertEqual(write_set.query_artifact.purpose, "QUERY")
        self.assertEqual(write_set.initial_input.contract_version, "task_input_snapshot_v1")
        self.assertEqual(write_set.task_created_event.event_type, "TASK_CREATED")
        self.assertEqual(write_set.starter_outbox.destination, "TEMPORAL_START")
        self.assertEqual(write_set.idempotency.scope, "CREATE_TASK")
        self.assertEqual(write_set.idempotency.key_hash_version, "hmac_sha256_v1")
        expected_key_digest = hmac.new(SECRET, b"key-1", hashlib.sha256).hexdigest()
        self.assertEqual(write_set.idempotency.key_digest, expected_key_digest)

    async def test_replay_returns_committed_task_id_not_local(self) -> None:
        subject = make_subject()
        conversation = make_conversation(subject.organization_id)
        committed = uuid4()
        service, transaction = self._service(
            subject,
            conversation,
            committed_task_id=committed,
            disposition=TaskCreationDisposition.REPLAYED,
        )

        result = await service.create(
            make_request(conversation.conversation_id), subject, idempotency_key="k"
        )

        self.assertEqual(result.disposition, "REPLAYED")
        self.assertEqual(result.task_id, committed)
        self.assertNotEqual(result.task_id, transaction.write_set.task.id)  # type: ignore[attr-defined]

    async def test_missing_conversation_is_not_found(self) -> None:
        subject = make_subject()
        service, _ = self._service(subject, None)
        with self.assertRaises(TaskEntryError) as caught:
            await service.create(make_request(uuid4()), subject, idempotency_key="k")
        self.assertEqual(caught.exception.code, "CONVERSATION_NOT_FOUND")

    async def test_cross_org_conversation_is_not_found(self) -> None:
        subject = make_subject()
        conversation = make_conversation(uuid4())
        service, _ = self._service(subject, conversation)
        with self.assertRaises(TaskEntryError) as caught:
            await service.create(
                make_request(conversation.conversation_id), subject, idempotency_key="k"
            )
        self.assertEqual(caught.exception.code, "CONVERSATION_NOT_FOUND")

    async def test_attachment_invalid_variants_are_rejected(self) -> None:
        subject = make_subject()
        conversation = make_conversation(subject.organization_id)
        artifact_id = uuid4()
        cases = [
            ("missing", None),
            ("cross_org", make_attachment(uuid4())),
            ("wrong_purpose", make_attachment(subject.organization_id, purpose="QUERY")),
            ("not_available", make_attachment(subject.organization_id, state="STAGED")),
            ("sha_mismatch", make_attachment(subject.organization_id, sha256="b" * 64)),
        ]
        for name, snapshot in cases:
            with self.subTest(name=name):
                attachments = {artifact_id: snapshot} if snapshot is not None else {}
                service, _ = self._service(subject, conversation, attachments)
                request = make_request(
                    conversation.conversation_id,
                    attachments=(
                        CreateTaskAttachmentV1(artifact_id=artifact_id, sha256="a" * 64, ordinal=1),
                    ),
                )
                with self.assertRaises(TaskEntryError) as caught:
                    await service.create(request, subject, idempotency_key="k")
                self.assertEqual(caught.exception.code, "TASK_ATTACHMENT_INVALID")

    async def test_attachment_binding_carries_task_and_message_refs(self) -> None:
        subject = make_subject()
        conversation = make_conversation(subject.organization_id)
        snapshot = make_attachment(subject.organization_id)
        service, transaction = self._service(
            subject, conversation, {snapshot.artifact_id: snapshot}
        )
        request = make_request(
            conversation.conversation_id,
            attachments=(
                CreateTaskAttachmentV1(
                    artifact_id=snapshot.artifact_id, sha256=snapshot.sha256, ordinal=1
                ),
            ),
        )

        await service.create(request, subject, idempotency_key="k")

        write_set = transaction.write_set  # type: ignore[attr-defined]
        self.assertEqual(len(write_set.attachment_bindings), 1)
        binding = write_set.attachment_bindings[0]
        self.assertEqual(binding.task_id, write_set.task.id)
        self.assertEqual(binding.introduced_by_message_id, write_set.query_message.id)
        self.assertEqual(binding.ordinal, 1)

    async def test_output_contract_ref_not_found_is_mapped(self) -> None:
        subject = make_subject()
        conversation = make_conversation(subject.organization_id)
        service, _ = self._service(subject, conversation)
        contract = OutputContractV1(
            format="JSON",
            delivery="INLINE",
            language="en",
            schema_ref=SchemaRefV1(schema_id="s", schema_version="v1", schema_digest="a" * 64),
            template_or_skill_ref=None,
            citations="REQUIRED",
            max_output_bytes=1000,
        )
        request = make_request(conversation.conversation_id, output_contract=contract)
        with self.assertRaises(TaskEntryError) as caught:
            await service.create(request, subject, idempotency_key="k")
        self.assertEqual(caught.exception.code, "OUTPUT_CONTRACT_REF_NOT_FOUND")


class TaskQueryServiceTests(unittest.IsolatedAsyncioTestCase):
    def _row(self, **overrides: object) -> TaskRowSnapshot:
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

    async def test_get_detail_returns_owned_task(self) -> None:
        subject = make_subject()
        row = self._row(owner_id=subject.user_id, organization_id=subject.organization_id)
        service = TaskQueryService(FakeTaskQueryGateway(row))
        detail = await service.get_detail(row.task_id, subject)
        self.assertEqual(detail.task_id, row.task_id)
        self.assertEqual(detail.status, row.status)

    async def test_get_detail_cross_owner_is_not_found(self) -> None:
        subject = make_subject()
        row = self._row(owner_id=uuid4())
        service = TaskQueryService(FakeTaskQueryGateway(row))
        with self.assertRaises(TaskEntryError) as caught:
            await service.get_detail(row.task_id, subject)
        self.assertEqual(caught.exception.code, "TASK_NOT_FOUND")

    async def test_get_detail_missing_is_not_found(self) -> None:
        subject = make_subject()
        service = TaskQueryService(FakeTaskQueryGateway(None))
        with self.assertRaises(TaskEntryError) as caught:
            await service.get_detail(uuid4(), subject)
        self.assertEqual(caught.exception.code, "TASK_NOT_FOUND")

    async def test_get_result_returns_result_view(self) -> None:
        subject = make_subject()
        result_artifact_id = uuid4()
        row = self._row(
            owner_id=subject.user_id,
            organization_id=subject.organization_id,
            status="COMPLETED",
            result_artifact_id=result_artifact_id,
            completed_at=datetime.now(UTC),
        )
        service = TaskQueryService(FakeTaskQueryGateway(row))
        view = await service.get_result(row.task_id, subject)
        self.assertEqual(view.task_id, row.task_id)
        self.assertEqual(view.result_artifact_id, result_artifact_id)
        self.assertEqual(view.error_code, None)


if __name__ == "__main__":
    unittest.main()
