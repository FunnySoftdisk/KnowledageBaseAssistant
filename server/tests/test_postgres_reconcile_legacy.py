"""真实PostgreSQL上的旧0002 Goal/Plan对账与0003升级验收。

在独立一次性数据库上重放完整升级链：``0001→0002``、种入带悬空``task_attempt_id``的
遗留Goal/Plan及依赖子行、用对账工具归档并删除、再``0003→head``。只有对账工具真正
清空遗留行后，``0003``的失败关闭守卫才会放行，从而证明旧库升级工具可验收。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
from alembic import command
from alembic.config import Config

TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")
DIGEST = "a" * 64
SERVER_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = SERVER_ROOT.parent / "tools"


def _pg_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    return url


def _isolated_url(base_url: str, dbname: str) -> str:
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def _alembic_config() -> Config:
    return Config(str(SERVER_ROOT / "alembic.ini"))


def _load_reconcile_module():
    module_path = TOOLS_ROOT / "reconcile_legacy_goal_plan.py"
    spec = importlib.util.spec_from_file_location("reconcile_legacy_goal_plan", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 注册进sys.modules，否则dataclass在``from __future__ import annotations``下
    # 解析字符串注解时无法定位模块命名空间。
    sys.modules["reconcile_legacy_goal_plan"] = module
    spec.loader.exec_module(module)
    return module


def _seed_prerequisites(connection: psycopg.Connection, ids: dict[str, UUID]) -> None:
    connection.execute(
        "INSERT INTO iam.organization (id,name,status,version) VALUES (%s,'test','ACTIVE',1)",
        (ids["org"],),
    )
    connection.execute(
        "INSERT INTO iam.user_account (id,organization_id,username,normalized_username,"
        "display_name,password_hash,status,must_change_password,failed_login_count,"
        "credential_version,version) VALUES "
        "(%s,%s,'u','u','u','hash','ACTIVE',false,0,1,1)",
        (ids["user"], ids["org"]),
    )
    connection.execute(
        "INSERT INTO content.artifact (artifact_id,organization_id,owner_id,purpose,"
        "artifact_type,storage_backend,object_key,size_bytes,sha256,detected_media_type,"
        "schema_id,schema_version,schema_digest,data_labels_json,encryption_profile,state,"
        "legal_hold,row_version,created_by_actor_id) VALUES "
        "(%s,%s,%s,'QUERY','TEXT','S3',%s,1,%s,'text/plain','query','v1',%s,"
        "'{}'::jsonb,'dev','AVAILABLE',false,1,%s)",
        (
            ids["artifact"],
            ids["org"],
            ids["user"],
            f"test/{ids['artifact']}",
            DIGEST,
            DIGEST,
            ids["user"],
        ),
    )
    connection.execute(
        "INSERT INTO workflow.conversation (id,owner_id,organization_id,title,mode,"
        "conversation_memory_version,memory_policy) VALUES (%s,%s,%s,'test','internal',0,'OFF')",
        (ids["conversation"], ids["user"], ids["org"]),
    )
    connection.execute(
        "INSERT INTO workflow.intelligent_task (id,organization_id,owner_id,conversation_id,"
        "query_artifact_id,initial_input_id,current_input_id,authorization_ref_id,status,"
        "temporal_workflow_id,last_event_sequence,budget,budget_used,deadline_at,version) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'PLANNING',%s,0,'{}'::jsonb,'{}'::jsonb,"
        "now() + interval '1 hour',1)",
        (
            ids["task"],
            ids["org"],
            ids["user"],
            ids["conversation"],
            ids["artifact"],
            ids["input"],
            ids["input"],
            ids["dangling"],
            f"agent-task-v1-{ids['task']}",
        ),
    )
    connection.execute(
        "INSERT INTO workflow.conversation_message (id,conversation_id,role,message_kind,"
        "content_artifact_id,task_id,sequence,revision,content_hash,state) VALUES "
        "(%s,%s,'USER','USER_QUERY',%s,%s,1,1,%s,'ACTIVE')",
        (ids["message"], ids["conversation"], ids["artifact"], ids["task"], DIGEST),
    )
    connection.execute(
        "INSERT INTO workflow.task_input_snapshot (input_id,task_id,input_revision,message_id,"
        "message_artifact_id,actor_id,organization_id,conversation_id,attachment_binding_refs_json,"
        "knowledge_scope_json,network_policy,network_policy_source,output_contract_json,"
        "selected_resource_refs_json,task_constraint_refs_json,policy_snapshot_ref_json,"
        "locale,timezone,contract_version,snapshot_digest) VALUES "
        "(%s,%s,1,%s,%s,%s,%s,%s,'[]'::jsonb,'{}'::jsonb,'DENY','SYSTEM_DEFAULT',"
        "'{}'::jsonb,'[]'::jsonb,'[]'::jsonb,'{}'::jsonb,'zh-CN','Asia/Shanghai',"
        "'task_input_snapshot_v1',%s)",
        (
            ids["input"],
            ids["task"],
            ids["message"],
            ids["artifact"],
            ids["user"],
            ids["org"],
            ids["conversation"],
            DIGEST,
        ),
    )


def _seed_legacy_goal_plan(connection: psycopg.Connection, ids: dict[str, UUID]) -> None:
    connection.execute(
        "INSERT INTO workflow.task_goal_understanding (understanding_id,task_id,input_id,"
        "task_attempt_id,contract_version,prompt_profile,prompt_profile_digest,model_alias,"
        "goal_context_artifact_id,context_digest,raw_model_artifact_id,goal_artifact_id,"
        "understanding_digest,gate_profile,gate_profile_digest,gate_artifact_id,gate_digest,"
        "gate_outcome,subgoal_count,criterion_count,assumption_count,blocking_issue_count) "
        "VALUES (%s,%s,%s,%s,'supervisor_goal_understanding_v1',"
        "'supervisor-goal-understanding-v1',%s,'planning_strong',%s,%s,%s,%s,%s,"
        "'clarification_gate_v1',%s,%s,%s,'READY_TO_PLAN',1,1,0,0)",
        (
            ids["goal"],
            ids["task"],
            ids["input"],
            ids["dangling"],
            DIGEST,
            ids["artifact"],
            DIGEST,
            ids["artifact"],
            ids["artifact"],
            DIGEST,
            DIGEST,
            ids["artifact"],
            DIGEST,
        ),
    )
    connection.execute(
        "INSERT INTO workflow.task_plan_version (task_id,plan_version,predecessor_version,"
        "input_id,task_attempt_id,goal_understanding_id,planning_context_artifact_id,"
        "capability_planning_view_artifact_id,raw_model_artifact_id,validated_draft_artifact_id,"
        "validation_artifact_id,compiled_plan_artifact_id,compiled_plan_digest,revision_reason,"
        "status,created_by_agent_instance_id,activated_at) VALUES "
        "(%s,1,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'NEW_TASK','ACTIVE',%s,now())",
        (
            ids["task"],
            ids["input"],
            ids["dangling"],
            ids["goal"],
            ids["artifact"],
            ids["artifact"],
            ids["artifact"],
            ids["artifact"],
            ids["artifact"],
            ids["artifact"],
            DIGEST,
            ids["dangling"],
        ),
    )
    connection.execute(
        "INSERT INTO workflow.task_plan_item_definition (task_id,plan_version,plan_item_id,"
        "source_local_id,objective,executor_requirement,capability_requirements_json,"
        "candidate_capability_refs_json,input_artifact_refs_json,output_schema_ref_json,"
        "depends_on_item_ids_json,iteration_policy_json,initial_state,definition_digest) "
        "VALUES (%s,1,%s,'item-1','deliver','SUPERVISOR','[]'::jsonb,'[]'::jsonb,"
        "'[]'::jsonb,'{}'::jsonb,'[]'::jsonb,'{}'::jsonb,'READY',%s)",
        (ids["task"], ids["item"], DIGEST),
    )
    connection.execute(
        "INSERT INTO workflow.task_plan_hypothesis (task_id,plan_version,hypothesis_id,"
        "source_local_id,statement,initial_status,hypothesis_digest) VALUES "
        "(%s,1,%s,'h-1','assumption','OPEN',%s)",
        (ids["task"], ids["hypothesis"], DIGEST),
    )
    connection.execute(
        "INSERT INTO workflow.task_compiled_criterion (task_id,plan_version,criterion_id,"
        "statement,source_kind,source_refs,requirement_level,completion_role,protected,"
        "evaluation_mode,predicate,applicability,evaluator_profile,digest) VALUES "
        "(%s,1,%s,'deliverable','GOAL','[]'::jsonb,'REQUIRED','CORE_DELIVERABLE',true,"
        "'DETERMINISTIC','{}'::jsonb,'{}'::jsonb,'{}'::jsonb,%s)",
        (ids["task"], ids["criterion"], DIGEST),
    )
    connection.execute(
        "INSERT INTO workflow.plan_item_runtime (task_id,plan_version,plan_item_id,status,"
        "current_iteration,last_error_code,row_version) VALUES "
        "(%s,1,%s,'READY',0,NULL,1)",
        (ids["task"], ids["item"]),
    )


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class PostgresReconcileLegacyTests(unittest.TestCase):
    def setUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"reconcile_{os.getpid()}_{uuid4().hex[:8]}"
        self.isolated_url = _isolated_url(TEST_DATABASE_URL, self.dbname)
        with psycopg.connect(_pg_url(TEST_DATABASE_URL), autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{self.dbname}"')

    def tearDown(self) -> None:
        assert TEST_DATABASE_URL is not None
        with psycopg.connect(_pg_url(TEST_DATABASE_URL), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)')

    def _upgrade(self, revision: str) -> None:
        os.environ["KNOWLEDGE_DATABASE_URL"] = self.isolated_url
        command.upgrade(_alembic_config(), revision)

    def _seed(self) -> dict[str, UUID]:
        ids = {
            name: uuid4()
            for name in (
                "org",
                "user",
                "artifact",
                "conversation",
                "task",
                "message",
                "input",
                "goal",
                "dangling",
                "item",
                "hypothesis",
                "criterion",
            )
        }
        with psycopg.connect(_pg_url(self.isolated_url)) as connection:
            _seed_prerequisites(connection, ids)
            _seed_legacy_goal_plan(connection, ids)
            connection.commit()
        return ids

    def _run_reconcile(self, archive_dir: str) -> dict:
        module = _load_reconcile_module()
        report = module.inspect(self.isolated_url)
        self.assertEqual(report["legacy_goal_rows"], 1)
        self.assertEqual(report["legacy_plan_rows"], 1)
        return module.reconcile(self.isolated_url, Path(archive_dir))

    def test_legacy_goal_plan_are_reconciled_before_0003_upgrade(self) -> None:
        self._upgrade("0002")
        self._seed()

        with tempfile.TemporaryDirectory() as archive_dir:
            report = self._run_reconcile(archive_dir)
            self.assertEqual(report["action"], "reconciled")
            self.assertEqual(report["deleted_rows_by_table"]["workflow.task_plan_version"], 1)
            self.assertEqual(report["deleted_rows_by_table"]["workflow.task_goal_understanding"], 1)
            manifest = Path(archive_dir) / "manifest.json"
            self.assertTrue(manifest.exists())

        # 对账清空后，0003守卫放行并继续升级到head。
        self._upgrade("0003")
        self._upgrade("head")

        with psycopg.connect(_pg_url(self.isolated_url)) as connection:
            goal_rows = connection.execute(
                "SELECT count(*) FROM workflow.task_goal_understanding"
            ).fetchone()
            plan_rows = connection.execute(
                "SELECT count(*) FROM workflow.task_plan_version"
            ).fetchone()
        self.assertEqual(goal_rows[0], 0)
        self.assertEqual(plan_rows[0], 0)


if __name__ == "__main__":
    unittest.main()
