"""DEV-02 真实PostgreSQL端到端验收：认证、任务创建（幂等）与查询。

在隔离一次性数据库上以真实FastAPI应用（真实异步引擎 + 真实Repository/UoW）验证
DEV-02三条退出门槛，不注入任何Fake服务：

- 重放不重复建Task：同一幂等键+同一请求体重复提交返回同一task_id，且写集各表唯一；
- 所有者隔离：跨主体的任务读取折叠为TASK_NOT_FOUND，跨主体的会话写入口被拒绝；
- 事务故障不留半状态：幂等键复用冲突、附件/会话校验失败后，写集各表零残留。

认证链从登录签发HS256令牌到Subject按PG权威状态重校验全部走真实路径。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from knowledge_system.api.app import create_app
from knowledge_system.api.settings import SecretRef, Settings
from knowledge_system.modules.iam.public import Argon2PasswordHasher

TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")
SERVER_ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("TEST_JWT_SIGNING_SECRET", "j" * 64)
os.environ.setdefault("TEST_IDEMPOTENCY_HMAC_SECRET", "h" * 64)

# 无附件创建时固定落库一张记录的核心写集表；全有或全无断言以这些表为主。
_CORE_WRITE_TABLES = (
    "workflow.intelligent_task",
    "workflow.conversation_message",
    "workflow.task_input_snapshot",
    "workflow.task_event",
    "integration.outbox_message",
    "integration.idempotency_record",
)

# 全部可能被创建写集触碰的表；零残留断言覆盖附件绑定表（无附件时恒为零）。
_WRITE_SET_TABLES = _CORE_WRITE_TABLES + ("workflow.task_attachment_binding",)


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


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class PostgresDev02ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev02_{os.getpid()}_{uuid4().hex[:8]}"
        self.isolated_url = _isolated_url(TEST_DATABASE_URL, self.dbname)
        self.isolated_pg = _pg_url(self.isolated_url)
        with psycopg.connect(_pg_url(TEST_DATABASE_URL), autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{self.dbname}"')
        os.environ["KNOWLEDGE_DATABASE_URL"] = self.isolated_url
        command.upgrade(_alembic_config(), "head")

        self.org = uuid4()
        self.alice = uuid4()
        self.bob = uuid4()
        self.conv_a = uuid4()
        self.conv_b = uuid4()
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
            for user_id, username, conversation_id in (
                (self.alice, "alice", self.conv_a),
                (self.bob, "bob", self.conv_b),
            ):
                connection.execute(
                    "INSERT INTO iam.user_account "
                    "(id,organization_id,username,normalized_username,display_name,"
                    "password_hash,status,must_change_password,failed_login_count,"
                    "credential_version,version) VALUES "
                    "(%s,%s,%s,%s,%s,%s,'ACTIVE',false,0,1,1)",
                    (
                        user_id,
                        self.org,
                        username,
                        username,
                        username.title(),
                        password_hash,
                    ),
                )
                connection.execute(
                    "INSERT INTO workflow.conversation "
                    "(id,owner_id,organization_id,title,mode,"
                    "conversation_memory_version,memory_policy) VALUES "
                    "(%s,%s,%s,'test','internal',0,'OFF')",
                    (conversation_id, user_id, self.org),
                )
            connection.commit()

    def _client(self) -> TestClient:
        return TestClient(create_app(_settings(self.isolated_url)))

    def _login(self, client: TestClient, username: str) -> str:
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": "secret-password"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["access_token"]

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _create_body(conversation_id: object, query: str = "hello") -> dict[str, object]:
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

    def _count(self, table: str, column: str | None = None, value: object = None) -> int:
        with psycopg.connect(self.isolated_pg) as connection:
            if column is None:
                return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            return int(
                connection.execute(
                    f"SELECT count(*) FROM {table} WHERE {column} = %s", (value,)
                ).fetchone()[0]
            )

    def test_login_issues_token_and_me_resolves_subject_from_pg(self) -> None:
        with self._client() as client:
            token = self._login(client, "alice")
            resp = client.get("/api/v1/auth/me", headers=self._auth(token))
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["user_id"], str(self.alice))
            self.assertEqual(resp.json()["organization_id"], str(self.org))
            self.assertEqual(resp.json()["credential_version"], 1)

    def test_replay_same_idempotency_key_does_not_duplicate_task(self) -> None:
        with self._client() as client:
            token = self._login(client, "alice")
            key = str(uuid4())
            body = self._create_body(self.conv_a)
            first = client.post(
                "/api/v1/tasks",
                json=body,
                headers={**self._auth(token), "Idempotency-Key": key},
            )
            self.assertEqual(first.status_code, 202, first.text)
            first_task_id = first.json()["task_id"]

            second = client.post(
                "/api/v1/tasks",
                json=body,
                headers={**self._auth(token), "Idempotency-Key": key},
            )
            self.assertEqual(second.status_code, 202, second.text)
            self.assertEqual(second.json()["task_id"], first_task_id)

        for table in _CORE_WRITE_TABLES:
            self.assertEqual(self._count(table), 1, f"写集表{table}应恰好一行")
        self.assertEqual(self._count("workflow.task_attachment_binding"), 0)

    def test_idempotency_key_reuse_with_different_body_leaves_no_half_state(self) -> None:
        with self._client() as client:
            token = self._login(client, "alice")
            key = str(uuid4())
            first = client.post(
                "/api/v1/tasks",
                json=self._create_body(self.conv_a, "one"),
                headers={**self._auth(token), "Idempotency-Key": key},
            )
            self.assertEqual(first.status_code, 202, first.text)

            conflict = client.post(
                "/api/v1/tasks",
                json=self._create_body(self.conv_a, "two"),
                headers={**self._auth(token), "Idempotency-Key": key},
            )
            self.assertEqual(conflict.status_code, 409, conflict.text)
            self.assertEqual(conflict.json()["code"], "IDEMPOTENCY_KEY_REUSED")

        for table in _CORE_WRITE_TABLES:
            self.assertEqual(self._count(table), 1, f"冲突后写集表{table}仍应恰好一行")
        self.assertEqual(self._count("workflow.task_attachment_binding"), 0)

    def test_cross_owner_read_folds_to_not_found(self) -> None:
        with self._client() as client:
            alice_token = self._login(client, "alice")
            created = client.post(
                "/api/v1/tasks",
                json=self._create_body(self.conv_a),
                headers={**self._auth(alice_token), "Idempotency-Key": str(uuid4())},
            )
            self.assertEqual(created.status_code, 202, created.text)
            task_id = created.json()["task_id"]

            bob_token = self._login(client, "bob")
            detail = client.get(f"/api/v1/tasks/{task_id}", headers=self._auth(bob_token))
            self.assertEqual(detail.status_code, 404)
            self.assertEqual(detail.json()["code"], "TASK_NOT_FOUND")

            result = client.get(
                f"/api/v1/tasks/{task_id}/result", headers=self._auth(bob_token)
            )
            self.assertEqual(result.status_code, 404)
            self.assertEqual(result.json()["code"], "TASK_NOT_FOUND")

    def test_cross_owner_conversation_write_rejected_without_rows(self) -> None:
        with self._client() as client:
            bob_token = self._login(client, "bob")
            resp = client.post(
                "/api/v1/tasks",
                json=self._create_body(self.conv_a),
                headers={**self._auth(bob_token), "Idempotency-Key": str(uuid4())},
            )
            self.assertEqual(resp.status_code, 404, resp.text)
            self.assertEqual(resp.json()["code"], "CONVERSATION_NOT_FOUND")

        for table in _WRITE_SET_TABLES:
            self.assertEqual(self._count(table), 0, f"拒绝后写集表{table}应零行")

    def test_attachment_validation_failure_leaves_no_rows(self) -> None:
        with self._client() as client:
            token = self._login(client, "alice")
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
            self.assertEqual(resp.json()["code"], "TASK_ATTACHMENT_INVALID")

        for table in _WRITE_SET_TABLES:
            self.assertEqual(self._count(table), 0, f"校验失败后写集表{table}应零行")


if __name__ == "__main__":
    unittest.main()
