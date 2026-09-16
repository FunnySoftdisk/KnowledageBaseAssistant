"""可选真实PostgreSQL任务入口事务测试。"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from knowledge_system.infrastructure.persistence import DatabaseEngineSettings, build_async_engine

TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class PostgresTaskPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_message_and_initial_input_commit_as_one_deferred_transaction(self) -> None:
        assert TEST_DATABASE_URL is not None
        engine = build_async_engine(DatabaseEngineSettings(TEST_DATABASE_URL))
        ids = {
            name: uuid4()
            for name in ("org", "user", "artifact", "conversation", "task", "message", "input")
        }
        digest = "a" * 64
        now = datetime.now(UTC)

        try:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                await connection.execute(
                    text(
                        "INSERT INTO iam.organization "
                        "(id,name,status,version) VALUES (:id,'test','ACTIVE',1)"
                    ),
                    {"id": ids["org"]},
                )
                await connection.execute(
                    text(
                        "INSERT INTO iam.user_account "
                        "(id,organization_id,username,normalized_username,display_name,"
                        "password_hash,status,must_change_password,failed_login_count,"
                        "credential_version,version) VALUES "
                        "(:id,:org,'test','test','test','hash','ACTIVE',false,0,1,1)"
                    ),
                    {"id": ids["user"], "org": ids["org"]},
                )
                await connection.execute(
                    text(
                        "INSERT INTO content.artifact "
                        "(artifact_id,organization_id,owner_id,purpose,artifact_type,"
                        "storage_backend,object_key,size_bytes,sha256,detected_media_type,"
                        "schema_id,schema_version,schema_digest,data_labels_json,"
                        "encryption_profile,state,legal_hold,row_version,created_by_actor_id) "
                        "VALUES (:id,:org,:owner,'QUERY','TEXT','S3',:object_key,1,:digest,"
                        "'text/plain','query','v1',:digest,'{}'::jsonb,'dev','AVAILABLE',"
                        "false,1,:owner)"
                    ),
                    {
                        "id": ids["artifact"],
                        "org": ids["org"],
                        "owner": ids["user"],
                        "object_key": f"test/{ids['artifact']}",
                        "digest": digest,
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

                # Task在Input前插入，是本契约必须支持的真实循环外键路径。
                await connection.execute(
                    text(
                        "INSERT INTO workflow.intelligent_task "
                        "(id,organization_id,owner_id,conversation_id,query_artifact_id,"
                        "initial_input_id,current_input_id,authorization_ref_id,status,"
                        "temporal_workflow_id,last_event_sequence,budget,budget_used,"
                        "deadline_at,version) "
                        "VALUES (:id,:org,:owner,:conversation,:artifact,:input,:input,:auth,"
                        "'QUEUED',:workflow_id,0,'{}'::jsonb,'{}'::jsonb,:deadline,1)"
                    ),
                    {
                        "id": ids["task"],
                        "org": ids["org"],
                        "owner": ids["user"],
                        "conversation": ids["conversation"],
                        "artifact": ids["artifact"],
                        "input": ids["input"],
                        "auth": uuid4(),
                        "workflow_id": f"agent-task-v1-{ids['task']}",
                        "deadline": now + timedelta(hours=1),
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO workflow.conversation_message "
                        "(id,conversation_id,role,message_kind,content_artifact_id,task_id,"
                        "sequence,revision,content_hash,state) VALUES "
                        "(:id,:conversation,'USER','USER_QUERY',:artifact,:task,1,1,:digest,'ACTIVE')"
                    ),
                    {
                        "id": ids["message"],
                        "conversation": ids["conversation"],
                        "artifact": ids["artifact"],
                        "task": ids["task"],
                        "digest": digest,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO workflow.task_input_snapshot "
                        "(input_id,task_id,input_revision,message_id,message_artifact_id,actor_id,"
                        "organization_id,conversation_id,attachment_binding_refs_json,"
                        "knowledge_scope_json,network_policy,network_policy_source,"
                        "output_contract_json,selected_resource_refs_json,task_constraint_refs_json,"
                        "policy_snapshot_ref_json,locale,timezone,contract_version,"
                        "snapshot_digest) VALUES "
                        "(:id,:task,1,:message,:artifact,:actor,:org,:conversation,'[]'::jsonb,"
                        "'{}'::jsonb,'DENY','SYSTEM_DEFAULT','{}'::jsonb,'[]'::jsonb,'[]'::jsonb,"
                        "'{}'::jsonb,'zh-CN','Asia/Shanghai','task_input_snapshot_v1',:digest)"
                    ),
                    {
                        "id": ids["input"],
                        "task": ids["task"],
                        "message": ids["message"],
                        "artifact": ids["artifact"],
                        "actor": ids["user"],
                        "org": ids["org"],
                        "conversation": ids["conversation"],
                        "digest": digest,
                    },
                )
                await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                stored_input = await connection.scalar(
                    text("SELECT current_input_id FROM workflow.intelligent_task WHERE id=:id"),
                    {"id": ids["task"]},
                )
                self.assertEqual(stored_input, ids["input"])
                await transaction.rollback()
        finally:
            await engine.dispose()

    async def test_invalid_workflow_event_reference_shape_is_rejected(self) -> None:
        assert TEST_DATABASE_URL is not None
        engine = build_async_engine(DatabaseEngineSettings(TEST_DATABASE_URL))
        try:
            async with engine.begin() as connection:
                with self.assertRaisesRegex(IntegrityError, "workflow_event_has_business_ref"):
                    await connection.execute(
                        text(
                            "INSERT INTO workflow.task_event "
                            "(id,task_id,sequence,event_type,event_schema_version,workflow_relevant,"
                            "observation_expected_count,payload_json,payload_digest) VALUES "
                            "(:id,:task,1,'TEST','v1',true,0,'{}'::jsonb,:digest)"
                        ),
                        {"id": uuid4(), "task": UUID(int=0), "digest": "a" * 64},
                    )
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
