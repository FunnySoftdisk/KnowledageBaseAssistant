"""DEV-07 TEMPORAL_SIGNAL Outbox消费者真实PostgreSQL验收：取消 → 信号行 → 信号投递。

在隔离一次性数据库上以真实`SqlAlchemyOutboxRepository` + 真实API（登录/创建/取消）验证
取消信号消费者的纵向闭环，仅以`FakeSignaller`替换真实Temporal连接：

- 取消投递：API取消Task后`TEMPORAL_SIGNAL`发件行被认领，投递器收到
  `agent-task-v1-{task_id}` + `business_event_available_v1`，行置PUBLISHED；
- 投递失败重试：投递器失败时行按`max_attempts`回到PENDING，记录`TEMPORAL_SIGNAL_FAILED`。
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
from knowledge_system.modules.temporal.application.outbox_signal_publisher import (
    OutboxSignalPublisher,
)

TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")
SERVER_ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("TEST_JWT_SIGNING_SECRET", "j" * 64)
os.environ.setdefault("TEST_IDEMPOTENCY_HMAC_SECRET", "h" * 64)


class FakeSignaller:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def signal_task_workflow(self, workflow_id: str, signal_name: str) -> None:
        if self.fail:
            raise RuntimeError("TEMPORAL_SIGNAL_UNAVAILABLE")
        self.calls.append((workflow_id, signal_name))


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
class Dev07SignalPublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev07_signal_{os.getpid()}_{uuid4().hex[:8]}"
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

    def _login(self, client: TestClient) -> str:
        token_resp = client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "secret-password"},
        )
        self.assertEqual(token_resp.status_code, 200, token_resp.text)
        return token_resp.json()["access_token"]

    def _create_and_cancel_task(self, client: TestClient, token: str) -> UUID:
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
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(resp.status_code, 202, resp.text)
        task_id = UUID(resp.json()["task_id"])
        cancel = client.post(
            f"/api/v1/tasks/{task_id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(cancel.status_code, 200, cancel.text)
        return task_id

    def _signal_rows(self) -> list[dict]:
        with psycopg.connect(self.isolated_pg, row_factory=dict_row) as connection:
            return list(
                connection.execute(
                    "SELECT * FROM integration.outbox_message WHERE destination = 'TEMPORAL_SIGNAL'"
                )
            )

    def _poll(self, signaller: FakeSignaller, *, max_attempts: int = 8):
        engine = build_async_engine(DatabaseEngineSettings(url=self.isolated_url))
        session_factory = build_session_factory(engine)
        publisher = OutboxSignalPublisher(
            SqlAlchemyOutboxRepository(session_factory),
            signaller,
            max_attempts=max_attempts,
        )
        try:
            return asyncio.run(publisher.poll_once())
        finally:
            asyncio.run(engine.dispose())

    def test_cancel_delivers_signal_and_publishes(self) -> None:
        with TestClient(create_app(_settings(self.isolated_url))) as client:
            token = self._login(client)
            task_id = self._create_and_cancel_task(client, token)

        rows = self._signal_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["publish_status"], "PENDING")

        signaller = FakeSignaller()
        outcome = self._poll(signaller)
        self.assertEqual(outcome.claimed, 1)
        self.assertEqual(outcome.published, 1)
        self.assertEqual(
            signaller.calls,
            [(f"agent-task-v1-{task_id}", "business_event_available_v1")],
        )

        rows = self._signal_rows()
        self.assertEqual(rows[0]["publish_status"], "PUBLISHED")
        self.assertIsNotNone(rows[0]["published_at"])

    def test_signal_failure_returns_to_pending(self) -> None:
        with TestClient(create_app(_settings(self.isolated_url))) as client:
            token = self._login(client)
            self._create_and_cancel_task(client, token)

        signaller = FakeSignaller(fail=True)
        outcome = self._poll(signaller, max_attempts=8)
        self.assertEqual(outcome.retried, 1)
        self.assertEqual(outcome.dead, 0)

        rows = self._signal_rows()
        self.assertEqual(rows[0]["publish_status"], "PENDING")
        self.assertEqual(rows[0]["last_error_code"], "TEMPORAL_SIGNAL_FAILED")
        self.assertEqual(rows[0]["attempt_count"], 1)


if __name__ == "__main__":
    unittest.main()
