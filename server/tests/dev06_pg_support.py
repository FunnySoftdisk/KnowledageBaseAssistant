"""DEV-06 持久层真实 PG 测试的共享支撑：隔离库迁移与基础依赖链播种。

不包含 `test_` 前缀，不会被 pytest 收集；仅被三个 DEV-06 持久层验收模块导入。
基础链满足 `workflow.intelligent_task` 的外键闭包（组织/用户/会话/查询 Artifact/输入快照），
供 `task_runtime_state` / `task_observation` / 运行时事实只读仓库做真实库往返。
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

SERVER_ROOT = Path(__file__).resolve().parents[1]
DIGEST = "a" * 64
TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")


def pg_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    return url


def isolated_url(base_url: str, dbname: str) -> str:
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def alembic_config() -> Config:
    return Config(str(SERVER_ROOT / "alembic.ini"))


class IsolatedDatabase:
    """创建隔离数据库并 `alembic upgrade head`；`__aenter__` 返回 psycopg+psycopg URL。"""

    def __init__(self, base_url: str, dbname: str) -> None:
        self.base_url = base_url
        self.dbname = dbname
        self.url = isolated_url(base_url, dbname)

    async def __aenter__(self) -> str:
        with psycopg.connect(pg_url(self.base_url), autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{self.dbname}"')
        previous = os.environ.get("KNOWLEDGE_DATABASE_URL")
        os.environ["KNOWLEDGE_DATABASE_URL"] = self.url
        try:
            await asyncio.to_thread(command.upgrade, alembic_config(), "head")
        finally:
            if previous is None:
                os.environ.pop("KNOWLEDGE_DATABASE_URL", None)
            else:
                os.environ["KNOWLEDGE_DATABASE_URL"] = previous
        return self.url

    async def __aexit__(self, *exc: object) -> None:
        with psycopg.connect(pg_url(self.base_url), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)')


async def seed_foundation(
    engine: AsyncEngine,
    ids: dict[str, UUID],
    artifacts: dict[str, UUID],
    *,
    status: str = "PLANNING",
    active_plan_version: int | None = None,
) -> None:
    """播种任务行依赖链：组织/用户/Artifact/会话/消息/输入快照/任务行。"""

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
        for artifact_id in artifacts.values():
            await connection.execute(
                text(
                    "INSERT INTO content.artifact "
                    "(artifact_id,organization_id,owner_id,purpose,artifact_type,storage_backend,"
                    "object_key,size_bytes,sha256,detected_media_type,schema_id,schema_version,"
                    "schema_digest,data_labels_json,encryption_profile,state,legal_hold,row_version,"
                    "created_by_actor_id) VALUES "
                    "(:id,:org,:owner,'QUERY','TEXT','S3',:object_key,1,:digest,'text/plain',"
                    "'schema','v1',:digest,'{}'::jsonb,'dev','AVAILABLE',false,1,:owner)"
                ),
                {
                    "id": artifact_id,
                    "org": ids["org"],
                    "owner": ids["user"],
                    "object_key": f"test/{artifact_id}",
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
                "temporal_workflow_id,last_event_sequence,budget,budget_used,deadline_at,"
                "active_plan_version,version) VALUES "
                "(:id,:org,:owner,:conversation,:query,:input,:input,:auth,:status,"
                ":temporal,0,'{}'::jsonb,'{}'::jsonb,:deadline,:plan,1)"
            ),
            {
                "id": ids["task"],
                "org": ids["org"],
                "owner": ids["user"],
                "conversation": ids["conversation"],
                "query": artifacts["query"],
                "input": ids["input"],
                "auth": uuid4(),
                "status": status,
                "temporal": f"agent-task-v1-{ids['task']}",
                "deadline": deadline,
                "plan": active_plan_version,
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
                "artifact": artifacts["query"],
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
                "(:input,:task,1,:message,:query,:owner,:org,:conversation,"
                "'[]'::jsonb,'{}'::jsonb,'DENY','SYSTEM_DEFAULT','{}'::jsonb,"
                "'[]'::jsonb,'[]'::jsonb,'{}'::jsonb,'zh-CN','Asia/Shanghai',"
                "'task_input_snapshot_v1',:digest)"
            ),
            {
                "input": ids["input"],
                "task": ids["task"],
                "message": ids["message"],
                "query": artifacts["query"],
                "owner": ids["user"],
                "org": ids["org"],
                "conversation": ids["conversation"],
                "digest": DIGEST,
            },
        )


__all__ = [
    "DIGEST",
    "IsolatedDatabase",
    "SERVER_ROOT",
    "TEST_DATABASE_URL",
    "alembic_config",
    "isolated_url",
    "pg_url",
    "seed_foundation",
]
