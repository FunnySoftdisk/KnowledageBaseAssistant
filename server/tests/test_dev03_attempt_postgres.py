"""DEV-03C Attempt四重CAS真实PostgreSQL验收：认领铸币与旧Generation/迟到结果隔离。

在隔离一次性数据库上以真实`AttemptActivities`验证（认知Activity本批以替身替换，不调模型）：

- 认领铸币：`claim_next_attempt`以`FOR UPDATE SKIP LOCKED`把最早`CREATED` Attempt置
  `RUNNING`并递增`lease_generation`/`row_version`，二者作为fencing令牌随`AttemptDispatchV1`
  返回Root；
- 四重CAS：`commit_attempt_terminal_state`仅当`task_attempt_id + lease_generation +
  row_version + RUNNING`全部匹配才提交，冲突时精确区分旧Generation/行版本/非RUNNING/不存在；
- 隔离闭环：Attempt被重新调度（Generation递增）后，旧Child持旧令牌提交被`STALE_LEASE_GENERATION`
  拒绝，迟到结果不进PG。
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from sqlalchemy import text

from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.modules.temporal.domain.contracts import (
    AttemptKindV1,
    AttemptTerminalCommitV1,
)
from knowledge_system.modules.temporal.workflows.attempt_activities import AttemptActivities

TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")
SERVER_ROOT = Path(__file__).resolve().parents[1]

DIGEST = "a" * 64


def _pg_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    return url


def _isolated_url(base_url: str, dbname: str) -> str:
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def _alembic_config() -> Config:
    return Config(str(SERVER_ROOT / "alembic.ini"))


def child_workflow_id(task_id: UUID, attempt_id: UUID) -> str:
    return f"agent-attempt-v1-{str(task_id).lower()}-{str(attempt_id).lower()}"


async def _seed(engine, ids: dict[str, UUID]) -> None:
    """落入Goal理解阶段已提交的依赖：org/user/artifact/conversation/task/input/agent/pack。"""

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO iam.organization (id,name,status,version) "
                "VALUES (:id,:name,'ACTIVE',1)"
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
        deadline = datetime.now(UTC) + timedelta(hours=1)
        await connection.execute(
            text(
                "INSERT INTO workflow.intelligent_task "
                "(id,organization_id,owner_id,conversation_id,query_artifact_id,"
                "initial_input_id,current_input_id,authorization_ref_id,status,"
                "temporal_workflow_id,last_event_sequence,budget,budget_used,deadline_at,version) "
                "VALUES (:id,:org,:owner,:conversation,:artifact,:input,:input,:auth,'PLANNING',"
                ":temporal,0,'{}'::jsonb,'{}'::jsonb,:deadline,1)"
            ),
            {
                "id": ids["task"],
                "org": ids["org"],
                "owner": ids["user"],
                "conversation": ids["conversation"],
                "artifact": ids["artifact"],
                "input": ids["input"],
                "auth": ids["agent"],
                "temporal": f"agent-task-v1-{ids['task']}",
                "deadline": deadline,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.conversation_message "
                "(id,conversation_id,role,message_kind,content_artifact_id,task_id,sequence,"
                "revision,content_hash,state) VALUES "
                "(:id,:conversation,'USER','USER_QUERY',:artifact,:task,1,1,:digest,'ACTIVE')"
            ),
            {
                "id": ids["message"],
                "conversation": ids["conversation"],
                "artifact": ids["artifact"],
                "task": ids["task"],
                "digest": DIGEST,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.task_input_snapshot "
                "(input_id,task_id,input_revision,message_id,message_artifact_id,actor_id,"
                "organization_id,conversation_id,attachment_binding_refs_json,"
                "knowledge_scope_json,network_policy,network_policy_source,output_contract_json,"
                "selected_resource_refs_json,task_constraint_refs_json,policy_snapshot_ref_json,"
                "locale,timezone,contract_version,snapshot_digest) VALUES "
                "(:input,:task,1,:message,:artifact,:owner,:org,:conversation,"
                "'[]'::jsonb,'{}'::jsonb,'DENY','SYSTEM_DEFAULT','{}'::jsonb,"
                "'[]'::jsonb,'[]'::jsonb,'{}'::jsonb,'zh-CN','Asia/Shanghai',"
                "'task_input_snapshot_v1',:digest)"
            ),
            {
                "input": ids["input"],
                "task": ids["task"],
                "message": ids["message"],
                "artifact": ids["artifact"],
                "owner": ids["user"],
                "org": ids["org"],
                "conversation": ids["conversation"],
                "digest": DIGEST,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.agent_instance "
                "(id,task_id,parent_agent_instance_id,manifest_id,manifest_version,kind,"
                "delegated_scope,status,expires_at,created_reason,created_reason_hash,"
                "completed_at) VALUES "
                "(:id,:task,NULL,'supervisor','v1','SUPERVISOR','{}'::jsonb,'ACTIVE',NULL,"
                "'goal understanding',:digest,NULL)"
            ),
            {"id": ids["agent"], "task": ids["task"], "digest": DIGEST},
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.context_pack "
                "(id,task_id,action_attempt_id,created_by_attempt_id,conversation_id,actor_id,"
                "authorization_ref_id,authorization_version,state,context_policy_version,"
                "conversation_memory_version,user_private_memory_version,task_memory_version,"
                "runtime_version,plan_version,agent_manifest_version,model_revision,prompt_version,"
                "tool_schema_versions,output_schema_id,output_schema_version,tokenizer_revision,"
                "chat_template_hash,max_context,reserved_output,protocol_overhead,safety_margin,"
                "input_tokens,body_artifact_id,pack_hash,input_digest,consumed_at) VALUES "
                "(:id,:task,NULL,NULL,:conversation,:actor,NULL,NULL,'READY','context-policy-v1',"
                "0,0,0,'runtime-v1',NULL,'supervisor','model-1','prompt-v1','{}'::jsonb,"
                "NULL,NULL,'tok-1',:digest,8192,1024,512,256,100,:artifact,:digest,:digest,NULL)"
            ),
            {
                "id": ids["pack"],
                "task": ids["task"],
                "conversation": ids["conversation"],
                "actor": ids["user"],
                "artifact": ids["artifact"],
                "digest": DIGEST,
            },
        )


async def _insert_attempt(
    engine,
    ids: dict[str, UUID],
    *,
    attempt_id: UUID,
    status: str,
    lease_generation: int,
    row_version: int,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO workflow.task_attempt "
                "(id,task_id,input_id,plan_version,attempt_kind,attempt_no,conversation_id,"
                "agent_instance_id,agent_registration_key,manifest_id,manifest_version,"
                "manifest_digest,context_pack_id,context_pack_hash,grant_snapshot_digest,"
                "input_schema_version,input_digest,pydantic_run_id,temporal_child_workflow_id,"
                "lease_generation,status,external_effect_state,row_version) VALUES "
                "(:id,:task,:input,1,'PLAN',1,:conversation,:agent,'supervisor','supervisor',"
                "'v1',:digest,:pack,:digest,:digest,'goal-input-v1',:digest,:pydantic,:child,"
                ":lease,:status,'NO_EFFECT',:row_version)"
            ),
            {
                "id": attempt_id,
                "task": ids["task"],
                "input": ids["input"],
                "conversation": ids["conversation"],
                "agent": ids["agent"],
                "pack": ids["pack"],
                "digest": DIGEST,
                "pydantic": f"run-{attempt_id}",
                "child": child_workflow_id(ids["task"], attempt_id),
                "lease": lease_generation,
                "status": status,
                "row_version": row_version,
            },
        )


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class Dev03AttemptPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev03_attempt_{os.getpid()}_{uuid4().hex[:8]}"
        self.isolated_url = _isolated_url(TEST_DATABASE_URL, self.dbname)
        self.isolated_pg = _pg_url(self.isolated_url)
        with psycopg.connect(_pg_url(TEST_DATABASE_URL), autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{self.dbname}"')
        previous_url = os.environ.get("KNOWLEDGE_DATABASE_URL")
        os.environ["KNOWLEDGE_DATABASE_URL"] = self.isolated_url
        try:
            await asyncio.to_thread(command.upgrade, _alembic_config(), "head")
        finally:
            if previous_url is None:
                os.environ.pop("KNOWLEDGE_DATABASE_URL", None)
            else:
                os.environ["KNOWLEDGE_DATABASE_URL"] = previous_url

        self.engine = build_async_engine(DatabaseEngineSettings(url=self.isolated_url))
        self.session_factory = build_session_factory(self.engine)
        self.activities = AttemptActivities(self.session_factory)

        self.ids = {
            name: uuid4()
            for name in (
                "org",
                "user",
                "artifact",
                "conversation",
                "task",
                "message",
                "input",
                "agent",
                "pack",
            )
        }
        await _seed(self.engine, self.ids)
        self.task_id = self.ids["task"]

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        assert TEST_DATABASE_URL is not None
        with psycopg.connect(_pg_url(TEST_DATABASE_URL), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)')

    def _attempt_row(self, attempt_id: UUID) -> dict:
        with psycopg.connect(self.isolated_pg, row_factory=dict_row) as connection:
            rows = list(
                connection.execute(
                    "SELECT status, lease_generation, row_version, result_digest "
                    "FROM workflow.task_attempt WHERE id = %s",
                    (attempt_id,),
                )
            )
        return rows[0]

    async def test_claim_claims_created_and_mints_fencing_tokens(self) -> None:
        attempt_id = uuid4()
        await _insert_attempt(
            self.engine,
            self.ids,
            attempt_id=attempt_id,
            status="CREATED",
            lease_generation=0,
            row_version=1,
        )

        dispatch = await self.activities.claim_next_attempt(self.task_id)

        self.assertIsNotNone(dispatch)
        assert dispatch is not None
        self.assertEqual(dispatch.task_attempt_id, attempt_id)
        self.assertEqual(dispatch.attempt_kind, AttemptKindV1.PLAN)
        self.assertEqual(dispatch.plan_version, 1)
        self.assertEqual(dispatch.lease_generation, 1)
        self.assertEqual(dispatch.attempt_row_version, 2)

        row = self._attempt_row(attempt_id)
        self.assertEqual(row["status"], "RUNNING")
        self.assertEqual(row["lease_generation"], 1)
        self.assertEqual(row["row_version"], 2)

    async def test_claim_returns_none_when_no_created_attempt(self) -> None:
        attempt_id = uuid4()
        await _insert_attempt(
            self.engine,
            self.ids,
            attempt_id=attempt_id,
            status="RUNNING",
            lease_generation=1,
            row_version=2,
        )

        dispatch = await self.activities.claim_next_attempt(self.task_id)

        self.assertIsNone(dispatch)

    async def test_commit_cas_commits_with_correct_tokens(self) -> None:
        attempt_id = uuid4()
        await _insert_attempt(
            self.engine,
            self.ids,
            attempt_id=attempt_id,
            status="RUNNING",
            lease_generation=1,
            row_version=2,
        )

        outcome = await self.activities.commit_attempt_terminal_state(
            AttemptTerminalCommitV1(
                task_id=self.task_id,
                task_attempt_id=attempt_id,
                lease_generation=1,
                expected_row_version=2,
                terminal_state="COMPLETED",
                result_kind="PLAN_RESULT",
                result_schema_version="v1",
                result_digest="b" * 64,
            )
        )

        self.assertTrue(outcome.committed)
        self.assertIsNone(outcome.conflict_reason)
        row = self._attempt_row(attempt_id)
        self.assertEqual(row["status"], "COMPLETED")
        self.assertEqual(row["row_version"], 3)
        self.assertEqual(row["result_digest"], "b" * 64)

    async def test_commit_rejects_stale_lease_generation(self) -> None:
        attempt_id = uuid4()
        await _insert_attempt(
            self.engine,
            self.ids,
            attempt_id=attempt_id,
            status="RUNNING",
            lease_generation=2,
            row_version=3,
        )

        outcome = await self.activities.commit_attempt_terminal_state(
            AttemptTerminalCommitV1(
                task_id=self.task_id,
                task_attempt_id=attempt_id,
                lease_generation=1,
                expected_row_version=3,
                terminal_state="COMPLETED",
            )
        )

        self.assertFalse(outcome.committed)
        self.assertEqual(outcome.conflict_reason, "STALE_LEASE_GENERATION")
        self.assertEqual(self._attempt_row(attempt_id)["status"], "RUNNING")

    async def test_commit_rejects_row_version_conflict(self) -> None:
        attempt_id = uuid4()
        await _insert_attempt(
            self.engine,
            self.ids,
            attempt_id=attempt_id,
            status="RUNNING",
            lease_generation=1,
            row_version=3,
        )

        outcome = await self.activities.commit_attempt_terminal_state(
            AttemptTerminalCommitV1(
                task_id=self.task_id,
                task_attempt_id=attempt_id,
                lease_generation=1,
                expected_row_version=2,
                terminal_state="COMPLETED",
            )
        )

        self.assertFalse(outcome.committed)
        self.assertEqual(outcome.conflict_reason, "ROW_VERSION_CONFLICT")
        self.assertEqual(self._attempt_row(attempt_id)["status"], "RUNNING")

    async def test_commit_rejects_non_running_status(self) -> None:
        attempt_id = uuid4()
        await _insert_attempt(
            self.engine,
            self.ids,
            attempt_id=attempt_id,
            status="COMPLETED",
            lease_generation=1,
            row_version=2,
        )

        outcome = await self.activities.commit_attempt_terminal_state(
            AttemptTerminalCommitV1(
                task_id=self.task_id,
                task_attempt_id=attempt_id,
                lease_generation=1,
                expected_row_version=2,
                terminal_state="FAILED",
                error_code="LATE_RESULT",
                failure_class="BUSINESS",
            )
        )

        self.assertFalse(outcome.committed)
        self.assertEqual(outcome.conflict_reason, "NOT_RUNNING")

    async def test_commit_rejects_not_found(self) -> None:
        outcome = await self.activities.commit_attempt_terminal_state(
            AttemptTerminalCommitV1(
                task_id=self.task_id,
                task_attempt_id=uuid4(),
                lease_generation=1,
                expected_row_version=2,
                terminal_state="COMPLETED",
            )
        )

        self.assertFalse(outcome.committed)
        self.assertEqual(outcome.conflict_reason, "NOT_FOUND")

    async def test_stale_generation_is_fenced_end_to_end(self) -> None:
        """Attempt重新调度（Generation递增）后，旧Child持旧令牌提交被隔离。"""

        attempt_id = uuid4()
        await _insert_attempt(
            self.engine,
            self.ids,
            attempt_id=attempt_id,
            status="CREATED",
            lease_generation=0,
            row_version=1,
        )

        old_dispatch = await self.activities.claim_next_attempt(self.task_id)
        assert old_dispatch is not None
        self.assertEqual(old_dispatch.lease_generation, 1)
        self.assertEqual(old_dispatch.attempt_row_version, 2)

        # 重新调度：手动回到CREATED再认领，铸造新Generation令牌。
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE workflow.task_attempt SET status = 'CREATED', "
                    "ended_at = NULL, result_digest = NULL WHERE id = :id"
                ),
                {"id": attempt_id},
            )
        new_dispatch = await self.activities.claim_next_attempt(self.task_id)
        assert new_dispatch is not None
        self.assertEqual(new_dispatch.lease_generation, 2)
        self.assertEqual(new_dispatch.attempt_row_version, 3)

        # 旧Child迟到：持Generation=1/row=2提交，被STALE_LEASE_GENERATION隔离。
        stale_outcome = await self.activities.commit_attempt_terminal_state(
            AttemptTerminalCommitV1(
                task_id=self.task_id,
                task_attempt_id=attempt_id,
                lease_generation=old_dispatch.lease_generation,
                expected_row_version=old_dispatch.attempt_row_version,
                terminal_state="COMPLETED",
                result_digest="c" * 64,
            )
        )
        self.assertFalse(stale_outcome.committed)
        self.assertEqual(stale_outcome.conflict_reason, "STALE_LEASE_GENERATION")

        # 新Generation仍可正常提交，旧结果未污染PG。
        fresh_outcome = await self.activities.commit_attempt_terminal_state(
            AttemptTerminalCommitV1(
                task_id=self.task_id,
                task_attempt_id=attempt_id,
                lease_generation=new_dispatch.lease_generation,
                expected_row_version=new_dispatch.attempt_row_version,
                terminal_state="COMPLETED",
                result_digest="d" * 64,
            )
        )
        self.assertTrue(fresh_outcome.committed)
        row = self._attempt_row(attempt_id)
        self.assertEqual(row["status"], "COMPLETED")
        self.assertEqual(row["result_digest"], "d" * 64)


if __name__ == "__main__":
    unittest.main()
