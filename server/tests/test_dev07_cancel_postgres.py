"""DEV-07 取消写集的真实 PostgreSQL 事务测试（可选）。"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    SqlAlchemyUnitOfWork,
    TaskCancelDisposition,
    TaskCancellationTransactionService,
    TaskCancellationWriteSet,
    TaskCancelTerminalError,
    TaskCreationTransactionService,
    TaskCreationWriteSet,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.task_models import (
    ConversationMessageRecord,
    IdempotencyRecord,
    IntelligentTaskRecord,
    OutboxMessageRecord,
    TaskEventRecord,
    TaskInputSnapshotRecord,
)
from knowledge_system.modules.audit.domain.audit_event import AuditEventDraft, AuditResult
from knowledge_system.modules.audit.public import AuditService
from knowledge_system.modules.tasking.domain.core_types import canonical_json_sha256

TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")
DIGEST = "a" * 64


async def seed_foundation(engine: AsyncEngine, ids: dict[str, UUID]) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO iam.organization "
                "(id,name,status,version) VALUES (:id,:name,'ACTIVE',1)"
            ),
            {"id": ids["org"], "name": f"test-{ids['org']}"},
        )
        username = f"u-{ids['user']}"
        await connection.execute(
            text(
                "INSERT INTO iam.user_account "
                "(id,organization_id,username,normalized_username,display_name,password_hash,"
                "status,must_change_password,failed_login_count,credential_version,version) "
                "VALUES (:id,:org,:username,:username,:username,'hash','ACTIVE',false,0,1,1)"
            ),
            {"id": ids["user"], "org": ids["org"], "username": username},
        )
        await connection.execute(
            text(
                "INSERT INTO content.artifact "
                "(artifact_id,organization_id,owner_id,purpose,artifact_type,storage_backend,"
                "object_key,size_bytes,sha256,detected_media_type,schema_id,schema_version,"
                "schema_digest,data_labels_json,encryption_profile,state,legal_hold,row_version,"
                "created_by_actor_id) VALUES "
                "(:id,:org,:owner,'QUERY','TEXT','S3',:object_key,1,:digest,'text/plain',"
                "'query','v1',:digest,'{}'::jsonb,'dev','AVAILABLE',false,1,:owner)"
            ),
            {
                "id": ids["artifact"],
                "org": ids["org"],
                "owner": ids["user"],
                "object_key": f"test/{ids['artifact']}",
                "digest": DIGEST,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.conversation "
                "(id,owner_id,organization_id,title,mode,conversation_memory_version,"
                "memory_policy) VALUES (:id,:owner,:org,'test','internal',0,'OFF')"
            ),
            {"id": ids["conversation"], "owner": ids["user"], "org": ids["org"]},
        )


def new_ids() -> dict[str, UUID]:
    return {
        name: uuid4()
        for name in (
            "org",
            "user",
            "artifact",
            "conversation",
            "task",
            "message",
            "input",
            "event",
            "outbox",
            "idempotency",
        )
    }


def make_creation_write_set(ids: dict[str, UUID]) -> TaskCreationWriteSet:
    now = datetime.now(UTC)
    task = IntelligentTaskRecord(
        id=ids["task"],
        organization_id=ids["org"],
        owner_id=ids["user"],
        conversation_id=ids["conversation"],
        query_artifact_id=ids["artifact"],
        initial_input_id=ids["input"],
        current_input_id=ids["input"],
        authorization_ref_id=uuid4(),
        status="QUEUED",
        temporal_workflow_id=f"agent-task-v1-{ids['task']}",
        last_event_sequence=0,
        budget={},
        budget_used={},
        deadline_at=now + timedelta(hours=1),
        version=1,
    )
    message = ConversationMessageRecord(
        id=ids["message"],
        conversation_id=ids["conversation"],
        role="USER",
        message_kind="USER_QUERY",
        content_artifact_id=ids["artifact"],
        task_id=ids["task"],
        sequence=1,
        revision=1,
        content_hash=DIGEST,
        state="ACTIVE",
    )
    snapshot = TaskInputSnapshotRecord(
        input_id=ids["input"],
        task_id=ids["task"],
        input_revision=1,
        message_id=ids["message"],
        message_artifact_id=ids["artifact"],
        actor_id=ids["user"],
        organization_id=ids["org"],
        conversation_id=ids["conversation"],
        attachment_binding_refs_json=[],
        knowledge_scope_json={},
        network_policy="DENY",
        network_policy_source="SYSTEM_DEFAULT",
        output_contract_json={},
        selected_resource_refs_json=[],
        task_constraint_refs_json=[],
        policy_snapshot_ref_json={},
        locale="zh-CN",
        timezone="Asia/Shanghai",
        contract_version="task_input_snapshot_v1",
        snapshot_digest=DIGEST,
    )
    event = TaskEventRecord(
        id=ids["event"],
        task_id=ids["task"],
        sequence=1,
        event_type="TASK_CREATED",
        event_schema_version="task_event_v1",
        workflow_relevant=True,
        business_ref_type="TASK",
        business_ref_id=ids["task"],
        business_version=1,
        observation_projection_profile_version=None,
        observation_expected_count=0,
        payload_json={"task_id": str(ids["task"]), "status": "QUEUED"},
        payload_digest=DIGEST,
    )
    outbox = OutboxMessageRecord(
        id=ids["outbox"],
        event_id=ids["event"],
        aggregate_type="TASK",
        aggregate_id=ids["task"],
        aggregate_version=1,
        destination="TEMPORAL_START",
        schema_version="agent_task_workflow_start_v1",
        payload_json={"task_id": str(ids["task"]), "event_id": str(ids["event"])},
        payload_digest=DIGEST,
        publish_status="PENDING",
        available_at=now,
        attempt_count=0,
        row_version=1,
    )
    idempotency = IdempotencyRecord(
        id=ids["idempotency"],
        scope="CREATE_TASK",
        actor_id=ids["user"],
        key_digest="1" * 64,
        key_hash_version="hmac_sha256_v1",
        request_hash="2" * 64,
        status="COMPLETED",
        resource_type="TASK",
        resource_id=ids["task"],
        response_code=202,
        response_body_json={"task_id": str(ids["task"]), "status": "QUEUED"},
        response_digest=DIGEST,
        expires_at=now + timedelta(days=1),
    )
    audit_event = AuditEventDraft(
        event_id=uuid4(),
        occurred_at=now,
        actor_id=ids["user"],
        actor_role_snapshot=(),
        session_id=None,
        source_ip=None,
        device_id=None,
        action="TASK_CREATE",
        resource_type="TASK",
        resource_id=str(ids["task"]),
        result=AuditResult.SUCCESS.value,
        reason_code=None,
        before_digest=None,
        after_digest=None,
        details_json=None,
        trace_id="a" * 32,
    )
    return TaskCreationWriteSet(
        task=task,
        query_message=message,
        initial_input=snapshot,
        attachment_bindings=(),
        task_created_event=event,
        starter_outbox=outbox,
        idempotency=idempotency,
        audit_event=audit_event,
    )


def make_cancel_write_set(task_id: UUID, actor_id: UUID) -> TaskCancellationWriteSet:
    now = datetime.now(UTC)
    event_id = uuid4()
    outbox_id = uuid4()
    payload = {"task_id": str(task_id), "status": "CANCEL_REQUESTED"}
    outbox_payload = {"task_id": str(task_id), "event_id": str(event_id)}
    event = TaskEventRecord(
        id=event_id,
        task_id=task_id,
        sequence=0,
        event_type="TASK_CANCELLED",
        event_schema_version="task_event_v1",
        workflow_relevant=True,
        business_ref_type="TASK",
        business_ref_id=task_id,
        business_version=None,
        observation_expected_count=0,
        payload_json=payload,
        payload_digest=canonical_json_sha256(payload),
    )
    outbox = OutboxMessageRecord(
        id=outbox_id,
        event_id=event_id,
        aggregate_type="TASK",
        aggregate_id=task_id,
        aggregate_version=0,
        destination="TEMPORAL_SIGNAL",
        schema_version="agent_task_workflow_signal_v1",
        payload_json=outbox_payload,
        payload_digest=canonical_json_sha256(outbox_payload),
        publish_status="PENDING",
        available_at=now,
        attempt_count=0,
        row_version=1,
    )
    audit_event = AuditEventDraft(
        event_id=uuid4(),
        occurred_at=now,
        actor_id=actor_id,
        actor_role_snapshot=(),
        session_id=None,
        source_ip=None,
        device_id=None,
        action="TASK_CANCEL",
        resource_type="TASK",
        resource_id=str(task_id),
        result=AuditResult.SUCCESS.value,
        reason_code=None,
        before_digest=None,
        after_digest=None,
        details_json=None,
        trace_id="b" * 32,
    )
    return TaskCancellationWriteSet(
        task_id=task_id,
        cancelled_event=event,
        signal_outbox=outbox,
        audit_event=audit_event,
    )


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class PostgresTaskCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.engine = build_async_engine(DatabaseEngineSettings(TEST_DATABASE_URL))
        self.session_factory = build_session_factory(self.engine)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    def creation_service(self) -> TaskCreationTransactionService:
        return TaskCreationTransactionService(
            lambda: SqlAlchemyUnitOfWork(self.session_factory), AuditService()
        )

    def cancellation_service(self) -> TaskCancellationTransactionService:
        return TaskCancellationTransactionService(
            lambda: SqlAlchemyUnitOfWork(self.session_factory), AuditService()
        )

    async def _create_open_task(self, ids: dict[str, UUID]) -> None:
        await seed_foundation(self.engine, ids)
        created = await self.creation_service().execute(make_creation_write_set(ids))
        assert created.task_id == ids["task"]

    async def test_cancel_records_event_outbox_and_audit(self) -> None:
        ids = new_ids()
        await self._create_open_task(ids)

        result = await self.cancellation_service().execute(
            make_cancel_write_set(ids["task"], ids["user"])
        )

        self.assertIs(result.disposition, TaskCancelDisposition.REQUESTED)
        self.assertEqual(result.status, "CANCEL_REQUESTED")

        async with self.engine.connect() as connection:
            status = await connection.scalar(
                text("SELECT status FROM workflow.intelligent_task WHERE id=:task"),
                {"task": ids["task"]},
            )
            last_sequence = await connection.scalar(
                text("SELECT last_event_sequence FROM workflow.intelligent_task WHERE id=:task"),
                {"task": ids["task"]},
            )
            cancel_event = await connection.execute(
                text(
                    "SELECT sequence, event_type, business_version FROM workflow.task_event "
                    "WHERE task_id=:task AND event_type='TASK_CANCELLED'"
                ),
                {"task": ids["task"]},
            )
            cancel_row = cancel_event.mappings().first()
            signal_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM integration.outbox_message "
                    "WHERE aggregate_id=:task AND destination='TEMPORAL_SIGNAL'"
                ),
                {"task": ids["task"]},
            )
            audit_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM audit.audit_event "
                    "WHERE resource_id=:task AND action='TASK_CANCEL'"
                ),
                {"task": str(ids["task"])},
            )

        self.assertEqual(status, "CANCEL_REQUESTED")
        self.assertEqual(last_sequence, 2)
        self.assertIsNotNone(cancel_row)
        assert cancel_row is not None
        self.assertEqual(cancel_row["sequence"], 2)
        self.assertEqual(cancel_row["business_version"], 2)
        self.assertEqual(signal_count, 1)
        self.assertEqual(audit_count, 1)

    async def test_cancel_is_idempotent_after_requested(self) -> None:
        ids = new_ids()
        await self._create_open_task(ids)

        first = await self.cancellation_service().execute(
            make_cancel_write_set(ids["task"], ids["user"])
        )
        second = await self.cancellation_service().execute(
            make_cancel_write_set(ids["task"], ids["user"])
        )

        self.assertIs(first.disposition, TaskCancelDisposition.REQUESTED)
        self.assertIs(second.disposition, TaskCancelDisposition.ALREADY_REQUESTED)
        self.assertEqual(second.status, "CANCEL_REQUESTED")

        async with self.engine.connect() as connection:
            cancel_events = await connection.scalar(
                text(
                    "SELECT count(*) FROM workflow.task_event "
                    "WHERE task_id=:task AND event_type='TASK_CANCELLED'"
                ),
                {"task": ids["task"]},
            )
            last_sequence = await connection.scalar(
                text("SELECT last_event_sequence FROM workflow.intelligent_task WHERE id=:task"),
                {"task": ids["task"]},
            )
        self.assertEqual(cancel_events, 1)
        self.assertEqual(last_sequence, 2)

    async def test_cancel_terminal_task_is_rejected(self) -> None:
        ids = new_ids()
        await self._create_open_task(ids)
        async with self.engine.begin() as connection:
            await connection.execute(
                text("UPDATE workflow.intelligent_task SET status='COMPLETED' WHERE id=:task"),
                {"task": ids["task"]},
            )

        with self.assertRaises(TaskCancelTerminalError):
            await self.cancellation_service().execute(
                make_cancel_write_set(ids["task"], ids["user"])
            )

    async def test_write_set_validate_relations_fails_closed(self) -> None:
        ids = new_ids()
        await self._create_open_task(ids)
        write_set = make_cancel_write_set(ids["task"], ids["user"])
        write_set.cancelled_event.event_type = "TASK_CREATED"

        with self.assertRaisesRegex(Exception, "TASK_CANCEL_EVENT_TYPE_INVALID"):
            await self.cancellation_service().execute(write_set)


if __name__ == "__main__":
    unittest.main()
