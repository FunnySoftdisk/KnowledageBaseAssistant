"""DEV-03B Outbox消费者真实PostgreSQL验收：Task创建 → Outbox → Temporal Root Starter。

在隔离一次性数据库上以真实`SqlAlchemyOutboxRepository` + 真实API创建的Task验证
发件箱消费者的纵向闭环，仅以`FakeStarter`替换真实Temporal连接（DEV-03E才接真实Temporal）：

- 认领发布：API创建Task后`TEMPORAL_START`发件行被认领，Starter收到由PG权威状态
  组装出的`AgentTaskWorkflowStartV1`（deadline_at + TASK_CREATED序列号），行置PUBLISHED；
- 启动失败重试置死：Starter持续失败时按`max_attempts`回到PENDING或置DEAD；
- 崩溃残留回收：`PUBLISHING`且租约过期的行被`FOR UPDATE SKIP LOCKED`重新认领；
- 载荷损坏置死：缺字段的发件载荷直接DEAD，不再无意义重试。
"""

from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from knowledge_system.api.app import create_app
from knowledge_system.api.settings import SecretRef, Settings
from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    SqlAlchemyOutboxRepository,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.modules.iam.public import Argon2PasswordHasher
from knowledge_system.modules.temporal.application.outbox_publisher import (
    TEMPORAL_START_FAILED,
    OutboxPublisher,
)
from knowledge_system.modules.temporal.domain.contracts import AgentTaskWorkflowArgsV1

TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")
SERVER_ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("TEST_JWT_SIGNING_SECRET", "j" * 64)
os.environ.setdefault("TEST_IDEMPOTENCY_HMAC_SECRET", "h" * 64)


class FakeStarter:
    """实现`TemporalWorkflowStarter`协议的测试替身；记录启动调用。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, AgentTaskWorkflowArgsV1]] = []

    async def start_task_workflow(self, workflow_id: str, args: AgentTaskWorkflowArgsV1) -> str:
        if self.fail:
            raise RuntimeError("TEMPORAL_UNAVAILABLE")
        self.calls.append((workflow_id, args))
        return "fake-run-id"


def _pg_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    return url


def _isolated_url(base_url: str, dbname: str) -> str:
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def _alembic_config() -> Config:
    return Config(str(SERVER_ROOT / "alembic.ini"))


def _settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        jwt_signing_secret_ref=SecretRef(kind="env", reference="TEST_JWT_SIGNING_SECRET"),
        idempotency_hmac_secret_ref=SecretRef(kind="env", reference="TEST_IDEMPOTENCY_HMAC_SECRET"),
    )


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class Dev03OutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev03_{os.getpid()}_{uuid4().hex[:8]}"
        self.isolated_url = _isolated_url(TEST_DATABASE_URL, self.dbname)
        self.isolated_pg = _pg_url(self.isolated_url)
        with psycopg.connect(_pg_url(TEST_DATABASE_URL), autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{self.dbname}"')
        self.previous_database_url = os.environ.get("KNOWLEDGE_DATABASE_URL")
        os.environ["KNOWLEDGE_DATABASE_URL"] = self.isolated_url
        command.upgrade(_alembic_config(), "head")

        self.org = uuid4()
        self.alice = uuid4()
        self.conv_a = uuid4()
        self._seed()

    def tearDown(self) -> None:
        assert TEST_DATABASE_URL is not None
        if self.previous_database_url is None:
            os.environ.pop("KNOWLEDGE_DATABASE_URL", None)
        else:
            os.environ["KNOWLEDGE_DATABASE_URL"] = self.previous_database_url
        with psycopg.connect(_pg_url(TEST_DATABASE_URL), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)')

    def _seed(self) -> None:
        password_hash = Argon2PasswordHasher().hash("secret-password")
        with psycopg.connect(self.isolated_pg) as connection:
            connection.execute(
                "INSERT INTO iam.organization (id,name,status,version) "
                "VALUES (%s,'test','ACTIVE',1)",
                (self.org,),
            )
            connection.execute(
                "INSERT INTO iam.user_account "
                "(id,organization_id,username,normalized_username,display_name,"
                "password_hash,status,must_change_password,failed_login_count,"
                "credential_version,version) VALUES "
                "(%s,%s,'alice','alice','Alice',%s,'ACTIVE',false,0,1,1)",
                (self.alice, self.org, password_hash),
            )
            connection.execute(
                "INSERT INTO workflow.conversation "
                "(id,owner_id,organization_id,title,mode,"
                "conversation_memory_version,memory_policy) VALUES "
                "(%s,%s,%s,'test','internal',0,'OFF')",
                (self.conv_a, self.alice, self.org),
            )
            connection.commit()

    def _create_task(self) -> UUID:
        with TestClient(create_app(_settings(self.isolated_url))) as client:
            token_resp = client.post(
                "/api/v1/auth/login",
                json={"username": "alice", "password": "secret-password"},
            )
            self.assertEqual(token_resp.status_code, 200, token_resp.text)
            token = token_resp.json()["access_token"]
            resp = client.post(
                "/api/v1/tasks",
                json={
                    "conversation_id": str(self.conv_a),
                    "query": "hello",
                    "attachments": [],
                    "knowledge_scope": None,
                    "output_contract": None,
                    "network_policy": None,
                    "selected_resource_refs": [],
                    "explicit_constraints": [],
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": str(uuid4()),
                },
            )
            self.assertEqual(resp.status_code, 202, resp.text)
            return UUID(resp.json()["task_id"])

    def _outbox_rows(self) -> list[dict]:
        with psycopg.connect(self.isolated_pg, row_factory=dict_row) as connection:
            return list(
                connection.execute(
                    "SELECT * FROM integration.outbox_message WHERE destination = 'TEMPORAL_START'"
                )
            )

    def _task_deadline(self, task_id: UUID):
        with psycopg.connect(self.isolated_pg) as connection:
            return connection.execute(
                "SELECT deadline_at FROM workflow.intelligent_task WHERE id = %s", (task_id,)
            ).fetchone()[0]

    def _poll(self, starter: FakeStarter, *, max_attempts: int = 8):
        engine = build_async_engine(DatabaseEngineSettings(url=self.isolated_url))
        session_factory = build_session_factory(engine)
        publisher = OutboxPublisher(
            SqlAlchemyOutboxRepository(session_factory),
            starter,
            max_attempts=max_attempts,
        )
        try:
            return asyncio.run(publisher.poll_once())
        finally:
            asyncio.run(engine.dispose())

    def test_task_creation_outbox_publishes_root_start(self) -> None:
        task_id = self._create_task()
        rows = self._outbox_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["publish_status"], "PENDING")

        starter = FakeStarter()
        outcome = self._poll(starter)
        self.assertEqual(outcome.claimed, 1)
        self.assertEqual(outcome.published, 1)

        self.assertEqual(len(starter.calls), 1)
        workflow_id, args = starter.calls[0]
        self.assertEqual(workflow_id, f"agent-task-v1-{task_id}")
        self.assertEqual(args.root.task.task_id, task_id)
        self.assertEqual(args.root.task_created_event.sequence, 1)
        self.assertEqual(args.root.run_sequence, 1)
        self.assertEqual(args.root.budget.wall_clock_deadline, self._task_deadline(task_id))

        rows = self._outbox_rows()
        self.assertEqual(rows[0]["publish_status"], "PUBLISHED")
        self.assertIsNotNone(rows[0]["published_at"])

    def test_start_failure_reaches_dead_without_infinite_retry(self) -> None:
        self._create_task()
        starter = FakeStarter(fail=True)
        outcome = self._poll(starter, max_attempts=1)
        self.assertEqual(outcome.claimed, 1)
        self.assertEqual(outcome.dead, 1)
        self.assertEqual(len(starter.calls), 0)

        rows = self._outbox_rows()
        self.assertEqual(rows[0]["publish_status"], "DEAD")
        self.assertEqual(rows[0]["last_error_code"], TEMPORAL_START_FAILED)
        self.assertEqual(rows[0]["attempt_count"], 1)

    def test_start_failure_within_budget_returns_to_pending(self) -> None:
        self._create_task()
        starter = FakeStarter(fail=True)
        outcome = self._poll(starter, max_attempts=8)
        self.assertEqual(outcome.retried, 1)
        rows = self._outbox_rows()
        self.assertEqual(rows[0]["publish_status"], "PENDING")
        self.assertIsNone(rows[0]["claimed_by"])
        self.assertEqual(rows[0]["attempt_count"], 1)
        self.assertGreater(rows[0]["available_at"], rows[0]["claimed_at"] or rows[0]["created_at"])

    def test_stale_publishing_row_is_reclaimed(self) -> None:
        task_id = self._create_task()
        with psycopg.connect(self.isolated_pg) as connection:
            connection.execute(
                "UPDATE integration.outbox_message SET publish_status = 'PUBLISHING', "
                "claimed_by = 'crashed-worker', claimed_at = now() - interval '2 hours', "
                "attempt_count = 3 WHERE destination = 'TEMPORAL_START'"
            )
            connection.commit()

        starter = FakeStarter()
        outcome = self._poll(starter)
        self.assertEqual(outcome.claimed, 1)
        self.assertEqual(outcome.published, 1)
        self.assertEqual(starter.calls[0][1].root.task.task_id, task_id)
        rows = self._outbox_rows()
        self.assertEqual(rows[0]["publish_status"], "PUBLISHED")
        self.assertEqual(rows[0]["attempt_count"], 4)

    def test_invalid_payload_marks_dead(self) -> None:
        self._create_task()
        with psycopg.connect(self.isolated_pg) as connection:
            connection.execute(
                "UPDATE integration.outbox_message SET payload_json = '{}'::jsonb "
                "WHERE destination = 'TEMPORAL_START'"
            )
            connection.commit()

        starter = FakeStarter()
        outcome = self._poll(starter)
        self.assertEqual(outcome.claimed, 1)
        self.assertEqual(outcome.dead, 1)
        self.assertEqual(len(starter.calls), 0)
        rows = self._outbox_rows()
        self.assertEqual(rows[0]["publish_status"], "DEAD")
        self.assertEqual(rows[0]["last_error_code"], "OUTBOX_PAYLOAD_INVALID")

    def test_payload_digest_mismatch_marks_dead(self) -> None:
        self._create_task()
        with psycopg.connect(self.isolated_pg) as connection:
            connection.execute(
                "UPDATE integration.outbox_message SET payload_json = "
                "jsonb_set(payload_json, '{task_id}', to_jsonb(%s::text)) "
                "WHERE destination = 'TEMPORAL_START'",
                (str(uuid4()),),
            )
            connection.commit()

        starter = FakeStarter()
        outcome = self._poll(starter)
        self.assertEqual(outcome.dead, 1)
        self.assertEqual(starter.calls, [])
        rows = self._outbox_rows()
        self.assertEqual(rows[0]["last_error_code"], "OUTBOX_PAYLOAD_INVALID")


if __name__ == "__main__":
    unittest.main()
