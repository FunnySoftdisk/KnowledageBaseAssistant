"""可选真实PostgreSQL Repository/UoW/幂等事务测试。"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    IdempotencyKeyReusedError,
    SqlAlchemyUnitOfWork,
    TaskCreationDisposition,
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


def make_write_set(
    ids: dict[str, UUID],
    *,
    key_digest: str,
    request_hash: str,
) -> TaskCreationWriteSet:
    now = datetime.now(UTC)
    response_body = {"task_id": str(ids["task"]), "status": "QUEUED"}
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
        key_digest=key_digest,
        key_hash_version="hmac_sha256_v1",
        request_hash=request_hash,
        status="COMPLETED",
        resource_type="TASK",
        resource_id=ids["task"],
        response_code=202,
        response_body_json=response_body,
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


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class PostgresTaskUnitOfWorkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.engine = build_async_engine(DatabaseEngineSettings(TEST_DATABASE_URL))
        self.session_factory = build_session_factory(self.engine)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    def service(self) -> TaskCreationTransactionService:
        return TaskCreationTransactionService(
            lambda: SqlAlchemyUnitOfWork(self.session_factory),
            AuditService(),
        )

    async def test_create_replay_and_conflicting_request_hash(self) -> None:
        ids = new_ids()
        await seed_foundation(self.engine, ids)
        key_digest = "b" * 64
        request_hash = "c" * 64
        write_set = make_write_set(ids, key_digest=key_digest, request_hash=request_hash)

        created = await self.service().execute(write_set)
        self.assertEqual(created.disposition, TaskCreationDisposition.CREATED)

        replay_ids = {
            **ids,
            **{
                name: uuid4()
                for name in ("task", "message", "input", "event", "outbox", "idempotency")
            },
        }
        replay_set = make_write_set(
            replay_ids,
            key_digest=key_digest,
            request_hash=request_hash,
        )
        replayed = await self.service().execute(replay_set)
        self.assertEqual(replayed.disposition, TaskCreationDisposition.REPLAYED)
        self.assertEqual(replayed.task_id, ids["task"])

        conflict_set = make_write_set(
            replay_ids,
            key_digest=key_digest,
            request_hash="d" * 64,
        )
        with self.assertRaisesRegex(IdempotencyKeyReusedError, "IDEMPOTENCY_KEY_REUSED"):
            await self.service().execute(conflict_set)

        async with self.engine.connect() as connection:
            task_count = await connection.scalar(
                text("SELECT count(*) FROM workflow.intelligent_task WHERE id IN (:a,:b)"),
                {"a": ids["task"], "b": replay_ids["task"]},
            )
            last_sequence = await connection.scalar(
                text("SELECT last_event_sequence FROM workflow.intelligent_task WHERE id=:task"),
                {"task": ids["task"]},
            )
            outbox_count = await connection.scalar(
                text("SELECT count(*) FROM integration.outbox_message WHERE aggregate_id=:task"),
                {"task": ids["task"]},
            )
        self.assertEqual(task_count, 1)
        self.assertEqual(last_sequence, 1)
        self.assertEqual(outbox_count, 1)

    async def test_exit_without_commit_rolls_back_complete_write_set(self) -> None:
        ids = new_ids()
        await seed_foundation(self.engine, ids)
        write_set = make_write_set(ids, key_digest="e" * 64, request_hash="f" * 64)

        async with SqlAlchemyUnitOfWork(self.session_factory) as unit_of_work:
            await unit_of_work.tasks.add_task_creation(write_set)
            await unit_of_work.tasks.flush()

        async with self.engine.connect() as connection:
            task_count = await connection.scalar(
                text("SELECT count(*) FROM workflow.intelligent_task WHERE id=:task"),
                {"task": ids["task"]},
            )
            event_count = await connection.scalar(
                text("SELECT count(*) FROM workflow.task_event WHERE task_id=:task"),
                {"task": ids["task"]},
            )
        self.assertEqual(task_count, 0)
        self.assertEqual(event_count, 0)

    async def test_concurrent_same_key_creates_exactly_one_task(self) -> None:
        ids = new_ids()
        await seed_foundation(self.engine, ids)
        other_ids = {
            **ids,
            **{
                name: uuid4()
                for name in ("task", "message", "input", "event", "outbox", "idempotency")
            },
        }
        key_digest = "1" * 64
        request_hash = "2" * 64
        first = make_write_set(ids, key_digest=key_digest, request_hash=request_hash)
        second = make_write_set(other_ids, key_digest=key_digest, request_hash=request_hash)

        results = await asyncio.gather(
            self.service().execute(first),
            self.service().execute(second),
        )
        self.assertEqual(
            {result.disposition for result in results},
            {TaskCreationDisposition.CREATED, TaskCreationDisposition.REPLAYED},
        )
        self.assertEqual(results[0].task_id, results[1].task_id)

        async with self.engine.connect() as connection:
            task_count = await connection.scalar(
                text("SELECT count(*) FROM workflow.intelligent_task WHERE id IN (:a,:b)"),
                {"a": ids["task"], "b": other_ids["task"]},
            )
            idempotency_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM integration.idempotency_record "
                    "WHERE actor_id=:actor AND key_digest=:key"
                ),
                {"actor": ids["user"], "key": key_digest},
            )
        self.assertEqual(task_count, 1)
        self.assertEqual(idempotency_count, 1)


if __name__ == "__main__":
    unittest.main()
