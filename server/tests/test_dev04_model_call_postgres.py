"""DEV-04B ModelCall账本真实PostgreSQL验收：幂等插入、状态CAS与对账读取。

在隔离一次性数据库上以真实`ModelCallLedgerRepository`验证：

- 插入按主键幂等：重复`model_call_id`返回既有行、不新建第二条计费行、不覆盖事实；
- 状态转移按`from_status + expected_row_version`CAS：正向链ACCEPTED→SENDING→STREAMING→
  COMPLETED，并落地`started_at/ended_at`；冲突精确区分不存在/非预期状态/行版本陈旧；
- 非法转移（终态再转移、跳过SENDING）以稳定`MODEL_CALL_STATE_TRANSITION_INVALID`失败关闭；
- Token Accounting/Usage分支判别列正确落库并可由CHECK约束拒绝非法分支。
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
from sqlalchemy.exc import IntegrityError

from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.model_call_repository import (
    ModelCallLedgerRepository,
)
from knowledge_system.modules.model_gateway.application.gateway import (
    ModelCallInsertDisposition,
    ModelCallTransitionConflict,
    ModelCallTransitionUpdates,
)
from knowledge_system.modules.model_gateway.domain.call_state import (
    ModelCallState,
    ModelCallStateViolation,
)
from knowledge_system.modules.model_gateway.domain.model_call import DataScope, ModelCallV1
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    ExactTokenAccountingV1,
)
from knowledge_system.modules.model_gateway.domain.usage import (
    ExactModelUsageV1,
    UnknownModelUsageV1,
)

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


async def _seed(engine, ids: dict[str, UUID]) -> None:
    """落入Context Pack已提交的依赖链：org/user/artifact/conversation/task/input/agent/pack。"""

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


def _accepted_call(*, context_pack_id: UUID, model_call_id: UUID | None = None) -> ModelCallV1:
    return ModelCallV1(
        model_call_id=model_call_id or uuid4(),
        context_pack_id=context_pack_id,
        capability_alias="planning_strong",
        approved_model_id="qwen-plus",
        approved_model_revision="rev1",
        prompt_version="v1",
        context_policy_version="context-policy-v1",
        authorization_version=1,
        runtime_version="runtime-v1",
        agent_manifest_version="supervisor",
        conversation_memory_version=0,
        user_private_memory_version=0,
        task_memory_version=0,
        token_accounting=ExactTokenAccountingV1(
            mode="EXACT",
            estimated=False,
            exact_input_tokens=128,
            tokenizer_revision="tok-1",
            chat_template_hash=DIGEST,
        ),
        data_scope=DataScope.INTERNAL_PRODUCTION,
        request_hash="b" * 64,
        usage=UnknownModelUsageV1(kind="UNKNOWN"),
        status=ModelCallState.ACCEPTED,
        created_at=datetime.now(UTC),
    )


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class Dev04ModelCallPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev04_model_call_{os.getpid()}_{uuid4().hex[:8]}"
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

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        assert TEST_DATABASE_URL is not None
        with psycopg.connect(_pg_url(TEST_DATABASE_URL), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)')

    async def _insert(self, call: ModelCallV1):
        async with self.session_factory() as session:
            repository = ModelCallLedgerRepository(session)
            result = await repository.insert_accepted(call)
            await session.commit()
        return result

    async def _transition(
        self,
        model_call_id: UUID,
        *,
        from_status: ModelCallState,
        to_status: ModelCallState,
        expected_row_version: int,
        updates: ModelCallTransitionUpdates | None = None,
    ):
        async with self.session_factory() as session:
            repository = ModelCallLedgerRepository(session)
            result = await repository.transition_status(
                model_call_id,
                from_status=from_status,
                to_status=to_status,
                expected_row_version=expected_row_version,
                updates=updates,
            )
            await session.commit()
        return result

    async def _get(self, model_call_id: UUID) -> ModelCallV1 | None:
        async with self.session_factory() as session:
            return await ModelCallLedgerRepository(session).get_by_id(model_call_id)

    def _model_call_row(self, model_call_id: UUID) -> dict:
        with psycopg.connect(self.isolated_pg, row_factory=dict_row) as connection:
            rows = list(
                connection.execute(
                    "SELECT status, row_version, usage_source, input_tokens, output_tokens, "
                    "token_accounting_kind, started_at, ended_at, provider_request_id, error_code "
                    "FROM workflow.model_call WHERE id = %s",
                    (model_call_id,),
                )
            )
        return rows[0]

    async def test_insert_accepted_and_read_back(self) -> None:
        call = _accepted_call(context_pack_id=self.ids["pack"])
        result = await self._insert(call)

        self.assertEqual(result.disposition, ModelCallInsertDisposition.INSERTED)
        self.assertEqual(result.row_version, 1)

        restored = await self._get(call.model_call_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored, call)

        row = self._model_call_row(call.model_call_id)
        self.assertEqual(row["status"], "ACCEPTED")
        self.assertEqual(row["token_accounting_kind"], "EXACT")
        self.assertEqual(row["usage_source"], "UNKNOWN")
        self.assertIsNone(row["input_tokens"])
        self.assertIsNone(row["output_tokens"])

    async def test_insert_is_idempotent_on_duplicate_id(self) -> None:
        model_call_id = uuid4()
        first = _accepted_call(context_pack_id=self.ids["pack"], model_call_id=model_call_id)
        second = _accepted_call(context_pack_id=self.ids["pack"], model_call_id=model_call_id)
        second = second.model_copy(update={"request_hash": "c" * 64})

        await self._insert(first)
        result = await self._insert(second)

        self.assertEqual(result.disposition, ModelCallInsertDisposition.ALREADY_PRESENT)
        self.assertEqual(result.call.request_hash, "b" * 64)
        self.assertEqual(result.row_version, 1)

        restored = await self._get(model_call_id)
        assert restored is not None
        self.assertEqual(restored.request_hash, "b" * 64)

    async def test_full_state_chain_accepted_to_completed(self) -> None:
        call = _accepted_call(context_pack_id=self.ids["pack"])
        await self._insert(call)

        sending = await self._transition(
            call.model_call_id,
            from_status=ModelCallState.ACCEPTED,
            to_status=ModelCallState.SENDING,
            expected_row_version=1,
            updates=ModelCallTransitionUpdates(
                actual_endpoint_id=uuid4(),
                actual_provider="dashscope",
                actual_model_id="qwen-plus",
                actual_model_revision="rev1",
                bifrost_request_id="bifrost-1",
                provider_request_id="provider-1",
            ),
        )
        self.assertTrue(sending.committed)
        self.assertEqual(sending.call.status, ModelCallState.SENDING)
        self.assertEqual(sending.call.provider_request_id, "provider-1")

        streaming = await self._transition(
            call.model_call_id,
            from_status=ModelCallState.SENDING,
            to_status=ModelCallState.STREAMING,
            expected_row_version=2,
        )
        self.assertTrue(streaming.committed)

        completed = await self._transition(
            call.model_call_id,
            from_status=ModelCallState.STREAMING,
            to_status=ModelCallState.COMPLETED,
            expected_row_version=3,
            updates=ModelCallTransitionUpdates(
                response_hash="d" * 64,
                usage=ExactModelUsageV1(
                    kind="EXACT", input_tokens=100, output_tokens=50, source="PROVIDER"
                ),
                duration_ms=1200,
            ),
        )
        self.assertTrue(completed.committed)

        row = self._model_call_row(call.model_call_id)
        self.assertEqual(row["status"], "COMPLETED")
        self.assertEqual(row["row_version"], 4)
        self.assertEqual(row["usage_source"], "PROVIDER")
        self.assertEqual(row["input_tokens"], 100)
        self.assertEqual(row["output_tokens"], 50)
        self.assertEqual(row["provider_request_id"], "provider-1")
        self.assertIsNotNone(row["started_at"])
        self.assertIsNotNone(row["ended_at"])

        final = await self._get(call.model_call_id)
        assert final is not None
        assert isinstance(final.usage, ExactModelUsageV1)
        self.assertEqual(final.usage.input_tokens, 100)

    async def test_transition_rejects_stale_row_version(self) -> None:
        call = _accepted_call(context_pack_id=self.ids["pack"])
        await self._insert(call)

        result = await self._transition(
            call.model_call_id,
            from_status=ModelCallState.ACCEPTED,
            to_status=ModelCallState.SENDING,
            expected_row_version=2,
        )

        self.assertFalse(result.committed)
        self.assertEqual(result.conflict_reason, ModelCallTransitionConflict.ROW_VERSION_CONFLICT)
        self.assertEqual(self._model_call_row(call.model_call_id)["status"], "ACCEPTED")

    async def test_transition_rejects_wrong_from_status(self) -> None:
        call = _accepted_call(context_pack_id=self.ids["pack"])
        await self._insert(call)

        result = await self._transition(
            call.model_call_id,
            from_status=ModelCallState.SENDING,
            to_status=ModelCallState.STREAMING,
            expected_row_version=1,
        )

        self.assertFalse(result.committed)
        self.assertEqual(result.conflict_reason, ModelCallTransitionConflict.NOT_IN_STATE)

    async def test_transition_rejects_not_found(self) -> None:
        result = await self._transition(
            uuid4(),
            from_status=ModelCallState.ACCEPTED,
            to_status=ModelCallState.SENDING,
            expected_row_version=1,
        )
        self.assertFalse(result.committed)
        self.assertEqual(result.conflict_reason, ModelCallTransitionConflict.NOT_FOUND)

    async def test_illegal_transition_raises_stable_error(self) -> None:
        call = _accepted_call(context_pack_id=self.ids["pack"])
        await self._insert(call)

        with self.assertRaises(ModelCallStateViolation) as raised:
            await self._transition(
                call.model_call_id,
                from_status=ModelCallState.ACCEPTED,
                to_status=ModelCallState.COMPLETED,
                expected_row_version=1,
            )
        self.assertEqual(raised.exception.code, "MODEL_CALL_STATE_TRANSITION_INVALID")

    async def test_get_by_provider_request_id(self) -> None:
        call = _accepted_call(context_pack_id=self.ids["pack"])
        await self._insert(call)
        await self._transition(
            call.model_call_id,
            from_status=ModelCallState.ACCEPTED,
            to_status=ModelCallState.SENDING,
            expected_row_version=1,
            updates=ModelCallTransitionUpdates(provider_request_id="provider-recon"),
        )

        async with self.session_factory() as session:
            repository = ModelCallLedgerRepository(session)
            restored = await repository.get_by_provider_request_id("provider-recon")

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.model_call_id, call.model_call_id)
        self.assertEqual(restored.status, ModelCallState.SENDING)

    async def test_token_accounting_branch_check_rejects_malformed_row(self) -> None:
        """EXACT分支缺tokenizer精确字段被CHECK拒绝（估算分支不伪造精确字段的反向约束）。"""

        model_call_id = uuid4()
        with self.assertRaises(IntegrityError):
            async with self.engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO workflow.model_call "
                        "(id,capability_alias,approved_model_id,approved_model_revision,"
                        "prompt_version,tool_schema_versions,context_pack_id,"
                        "context_policy_version,authorization_version,runtime_version,"
                        "agent_manifest_version,conversation_memory_version,"
                        "user_private_memory_version,task_memory_version,token_accounting_kind,"
                        "data_scope,request_hash,usage_source,status,row_version,created_at,"
                        "updated_at) VALUES "
                        "(:id,'planning_strong','qwen-plus','rev1','v1','{}'::jsonb,:pack,"
                        "'context-policy-v1',1,'runtime-v1','supervisor',0,0,0,'EXACT',"
                        "'INTERNAL_PRODUCTION',:digest,'UNKNOWN','ACCEPTED',1,now(),now())"
                    ),
                    {"id": model_call_id, "pack": self.ids["pack"], "digest": DIGEST},
                )


if __name__ == "__main__":
    unittest.main()
