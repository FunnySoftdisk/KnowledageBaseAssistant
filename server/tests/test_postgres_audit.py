"""DEV-02审计链真实PostgreSQL端到端验收：不可变追加、Hash链与同事务提交。

在隔离一次性数据库上以真实FastAPI应用验证审计链四条事实，不注入Fake：

- 追加不可变：应用只INSERT，UPDATE/DELETE被数据库Trigger拒绝；
- Hash链：`event_hash = SHA256(previous_hash + canonical_event)`逐条可复验；
- 同事务提交：Task创建成功时审计同事务可见；审计不可用时Task创建失败关闭不落半行；
- 登录成败审计：登录成功与失败各自追加SUCCESS/FAILURE审计事件。
"""

from __future__ import annotations

import hashlib
import os
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from knowledge_system.api.app import create_app
from knowledge_system.api.settings import SecretRef, Settings
from knowledge_system.modules.audit.domain.audit_event import AuditEventDraft
from knowledge_system.modules.iam.public import Argon2PasswordHasher

TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")
SERVER_ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("TEST_JWT_SIGNING_SECRET", "j" * 64)
os.environ.setdefault("TEST_IDEMPOTENCY_HMAC_SECRET", "h" * 64)


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
        idempotency_hmac_secret_ref=SecretRef(
            kind="env", reference="TEST_IDEMPOTENCY_HMAC_SECRET"
        ),
    )


def _draft_from_row(row: dict[str, Any]) -> AuditEventDraft:
    return AuditEventDraft(
        event_id=row["event_id"],
        occurred_at=row["occurred_at"],
        actor_id=row["actor_id"],
        actor_role_snapshot=tuple(row["actor_role_snapshot"] or ()),
        session_id=row["session_id"],
        source_ip=row["source_ip"],
        device_id=row["device_id"],
        action=row["action"],
        resource_type=row["resource_type"],
        resource_id=row["resource_id"],
        result=row["result"],
        reason_code=row["reason_code"],
        before_digest=row["before_digest"],
        after_digest=row["after_digest"],
        details_json=row["details_json"],
        trace_id=row["trace_id"],
    )


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class PostgresAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"audit_{os.getpid()}_{uuid4().hex[:8]}"
        self.isolated_url = _isolated_url(TEST_DATABASE_URL, self.dbname)
        self.isolated_pg = _pg_url(self.isolated_url)
        with psycopg.connect(_pg_url(TEST_DATABASE_URL), autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{self.dbname}"')
        os.environ["KNOWLEDGE_DATABASE_URL"] = self.isolated_url
        command.upgrade(_alembic_config(), "head")

        self.org = uuid4()
        self.alice = uuid4()
        self.conv_a = uuid4()
        self._seed()

    def tearDown(self) -> None:
        assert TEST_DATABASE_URL is not None
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

    def _client(self, *, raise_server_exceptions: bool = True) -> TestClient:
        return TestClient(
            create_app(_settings(self.isolated_url)),
            raise_server_exceptions=raise_server_exceptions,
        )

    def _login(
        self,
        client: TestClient,
        username: str = "alice",
        password: str = "secret-password",
    ) -> str:
        resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["access_token"]

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _create_body(conversation_id: UUID) -> dict[str, object]:
        return {
            "conversation_id": str(conversation_id),
            "query": "hello",
            "attachments": [],
            "knowledge_scope": None,
            "output_contract": None,
            "network_policy": None,
            "selected_resource_refs": [],
            "explicit_constraints": [],
        }

    def _audit_rows(self) -> list[dict[str, Any]]:
        with psycopg.connect(self.isolated_pg, row_factory=dict_row) as connection:
            return list(
                connection.execute(
                    "SELECT * FROM audit.audit_event ORDER BY occurred_at, event_id"
                )
            )

    def _audit_count(self, resource_type: str | None = None) -> int:
        with psycopg.connect(self.isolated_pg) as connection:
            if resource_type is None:
                return int(
                    connection.execute("SELECT count(*) FROM audit.audit_event").fetchone()[0]
                )
            return int(
                connection.execute(
                    "SELECT count(*) FROM audit.audit_event WHERE resource_type = %s",
                    (resource_type,),
                ).fetchone()[0]
            )

    def _chain_head(self) -> str | None:
        with psycopg.connect(self.isolated_pg) as connection:
            return connection.execute(
                "SELECT head_hash FROM audit.audit_chain_head WHERE chain_key = 'default'"
            ).fetchone()[0]

    def test_task_creation_appends_audit_atomically(self) -> None:
        with self._client() as client:
            token = self._login(client)
            resp = client.post(
                "/api/v1/tasks",
                json=self._create_body(self.conv_a),
                headers={**self._auth(token), "Idempotency-Key": str(uuid4())},
            )
            self.assertEqual(resp.status_code, 202, resp.text)
            task_id = resp.json()["task_id"]

        rows = self._audit_rows()
        task_audits = [r for r in rows if r["resource_type"] == "TASK"]
        self.assertEqual(len(task_audits), 1)
        audit = task_audits[0]
        self.assertEqual(audit["resource_id"], task_id)
        self.assertEqual(audit["result"], "SUCCESS")
        self.assertEqual(audit["action"], "TASK_CREATE")
        self.assertEqual(audit["actor_id"], self.alice)
        self.assertEqual(audit["trace_id"], resp.headers["X-Trace-Id"])
        # 链头指向最新事件Hash。
        self.assertEqual(self._chain_head(), audit["event_hash"])

    def test_audit_hash_chain_links_consecutive_events(self) -> None:
        with self._client() as client:
            token = self._login(client)
            client.post(
                "/api/v1/tasks",
                json=self._create_body(self.conv_a),
                headers={**self._auth(token), "Idempotency-Key": str(uuid4())},
            )

        rows = self._audit_rows()
        self.assertGreaterEqual(len(rows), 2)
        first, second = rows[0], rows[1]
        self.assertIsNone(first["previous_hash"])
        self.assertEqual(second["previous_hash"], first["event_hash"])
        self.assertEqual(self._chain_head(), rows[-1]["event_hash"])

    def test_audit_chain_recomputes_from_stored_rows(self) -> None:
        with self._client() as client:
            token = self._login(client)
            client.post(
                "/api/v1/tasks",
                json=self._create_body(self.conv_a),
                headers={**self._auth(token), "Idempotency-Key": str(uuid4())},
            )

        previous: str | None = None
        for row in self._audit_rows():
            canonical = _draft_from_row(row).canonical_bytes()
            expected = hashlib.sha256(
                ("" if previous is None else previous).encode("ascii") + canonical
            ).hexdigest()
            self.assertEqual(row["event_hash"], expected)
            self.assertEqual(row["previous_hash"], previous)
            previous = row["event_hash"]

    def test_audit_rows_are_immutable(self) -> None:
        with self._client() as client:
            self._login(client)

        row = self._audit_rows()[0]
        with psycopg.connect(self.isolated_pg) as connection:
            with self.assertRaises(Exception) as caught_update:
                connection.execute(
                    "UPDATE audit.audit_event SET action = 'TAMPERED' WHERE event_id = %s",
                    (row["event_id"],),
                )
            self.assertIn("AUDIT_EVENT_IMMUTABLE", str(caught_update.exception))
            connection.rollback()
            with self.assertRaises(Exception) as caught_delete:
                connection.execute(
                    "DELETE FROM audit.audit_event WHERE event_id = %s", (row["event_id"],)
                )
            self.assertIn("AUDIT_EVENT_IMMUTABLE", str(caught_delete.exception))
            connection.rollback()

    def test_login_success_and_failure_are_audited(self) -> None:
        with self._client() as client:
            succeeded = client.post(
                "/api/v1/auth/login",
                json={"username": "alice", "password": "secret-password"},
            )
            self.assertEqual(succeeded.status_code, 200, succeeded.text)
            failed = client.post(
                "/api/v1/auth/login", json={"username": "alice", "password": "wrong"}
            )
            self.assertEqual(failed.status_code, 401, failed.text)

        logins = [r for r in self._audit_rows() if r["action"] == "LOGIN"]
        self.assertEqual(len(logins), 2)
        self.assertEqual({r["result"] for r in logins}, {"SUCCESS", "FAILURE"})
        success = next(r for r in logins if r["result"] == "SUCCESS")
        failure = next(r for r in logins if r["result"] == "FAILURE")
        self.assertEqual(success["trace_id"], succeeded.headers["X-Trace-Id"])
        self.assertEqual(failure["trace_id"], failed.headers["X-Trace-Id"])
        self.assertEqual(failure["reason_code"], "AUTH_INVALID_CREDENTIALS")
        self.assertEqual(failure["resource_id"], str(self.alice))

    def test_failed_task_creation_leaves_no_audit_row(self) -> None:
        with self._client() as client:
            token = self._login(client)
            body = self._create_body(self.conv_a)
            body["attachments"] = [
                {"artifact_id": str(uuid4()), "sha256": "a" * 64, "ordinal": 1}
            ]
            resp = client.post(
                "/api/v1/tasks",
                json=body,
                headers={**self._auth(token), "Idempotency-Key": str(uuid4())},
            )
            self.assertEqual(resp.status_code, 422, resp.text)

        self.assertEqual(self._audit_count("TASK"), 0)

    def test_task_creation_fails_closed_when_audit_unavailable(self) -> None:
        # 审计不可用是未预期的服务器故障：默认TestClient会把ServerErrorMiddleware
        # 重新抛出的异常再抛给测试进程；此处关闭重抛，以便断言统一500信封而非异常。
        with self._client(raise_server_exceptions=False) as client:
            token = self._login(client)
            with psycopg.connect(self.isolated_pg) as connection:
                connection.execute("DELETE FROM audit.audit_chain_head WHERE chain_key = 'default'")
                connection.commit()
            resp = client.post(
                "/api/v1/tasks",
                json=self._create_body(self.conv_a),
                headers={**self._auth(token), "Idempotency-Key": str(uuid4())},
            )
            self.assertEqual(resp.status_code, 500, resp.text)
            self.assertEqual(resp.json()["code"], "INTERNAL_ERROR", resp.text)

        with psycopg.connect(self.isolated_pg) as connection:
            task_count = connection.execute(
                "SELECT count(*) FROM workflow.intelligent_task"
            ).fetchone()[0]
        self.assertEqual(task_count, 0, "审计不可用时Task创建必须失败关闭，不得留下半写Task")


if __name__ == "__main__":
    unittest.main()
