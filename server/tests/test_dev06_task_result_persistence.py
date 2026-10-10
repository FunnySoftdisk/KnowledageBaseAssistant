"""DEV-06 终态事务持久层验收：`workflow.task_result` 的 ORM/迁移/幂等提交语义。

离线部分只证明字段矩阵与 ORM 元数据一致、`record_from_commit` 把域写模型确定性压平为行、首版
`result_version=1` 之外被稳定契约错误拒绝；真实 PG 部分（`KNOWLEDGE_TEST_DATABASE_URL` 未配置时
跳过）验证「首次提交 INSERTED / 同一 Commit Digest 幂等重放 / 同版本不同 Digest 对账冲突且不覆盖」。
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
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from knowledge_system.infrastructure.persistence import (
    Base,
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.task_result_repository import (
    TaskResultCommitContractError,
    TaskResultRepository,
    record_from_commit,
)
from knowledge_system.modules.tasking.application.final_commit import (
    TaskResultCommitDisposition,
    TaskResultCommitV1,
)
from knowledge_system.modules.tasking.domain.final_gate import (
    ConstraintCheckV1,
    CriterionEvaluationStatusV1,
)
from knowledge_system.modules.tasking.domain.final_result import ResultCriterionCheckV1
from knowledge_system.modules.tasking.domain.runtime_decision import FinalOutcomeKindV1

DIGEST = "a" * 64
TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")
SERVER_ROOT = Path(__file__).resolve().parents[1]

PROJECTION = [
    "task_id",
    "contract_version",
    "result_version",
    "answer_status",
    "answer_artifact_id",
    "answer_sha256",
    "result_envelope_artifact_id",
    "citation_count",
    "result_artifact_count",
    "criterion_checks",
    "constraint_checks",
    "degradation_flags",
    "final_snapshot_id",
    "final_attempt_id",
    "candidate_artifact_id",
    "validation_artifact_id",
    "criterion_evaluation_set_artifact_id",
    "completion_gate_decision_artifact_id",
    "renderer_profile",
    "renderer_profile_digest",
    "result_commit_digest",
    "commit_receipt_id",
    "generated_at",
    "row_version",
]
DEFERRED_BARE = ("final_snapshot_id", "final_attempt_id", "commit_receipt_id")
REAL_FK_TARGETS = {
    "task_id": "intelligent_task",
    "answer_artifact_id": "artifact",
    "result_envelope_artifact_id": "artifact",
    "candidate_artifact_id": "artifact",
    "validation_artifact_id": "artifact",
    "criterion_evaluation_set_artifact_id": "artifact",
    "completion_gate_decision_artifact_id": "artifact",
}


def _criterion_check() -> ResultCriterionCheckV1:
    return ResultCriterionCheckV1(
        criterion_id=uuid4(),
        completion_role="CORE_DELIVERABLE",
        status=CriterionEvaluationStatusV1.SATISFIED,
        display_summary="答复满足核心交付物",
        citation_ids=("c_001",),
    )


def _fresh_artifact_ids() -> dict[str, UUID]:
    return {
        name: uuid4()
        for name in (
            "answer",
            "envelope",
            "candidate",
            "validation",
            "evaluation_set",
            "gate_decision",
        )
    }


def _commit(
    *,
    task_id: UUID,
    artifact_ids: dict[str, UUID],
    result_version: int = 1,
    result_commit_digest: str = "c" * 64,
) -> TaskResultCommitV1:
    return TaskResultCommitV1(
        task_id=task_id,
        result_version=result_version,
        answer_status=FinalOutcomeKindV1.COMPLETE,
        answer_artifact_id=artifact_ids["answer"],
        answer_sha256=DIGEST,
        result_envelope_artifact_id=artifact_ids["envelope"],
        citation_count=3,
        result_artifact_count=2,
        criterion_checks=(_criterion_check(),),
        constraint_checks=(ConstraintCheckV1(constraint_id=uuid4(), status="SATISFIED"),),
        degradation_flags=("STALE_INPUT",),
        final_snapshot_id=uuid4(),
        final_attempt_id=uuid4(),
        candidate_artifact_id=artifact_ids["candidate"],
        validation_artifact_id=artifact_ids["validation"],
        criterion_evaluation_set_artifact_id=artifact_ids["evaluation_set"],
        completion_gate_decision_artifact_id=artifact_ids["gate_decision"],
        renderer_profile="final-answer-markdown-v1",
        renderer_profile_digest="b" * 64,
        result_commit_digest=result_commit_digest,
        commit_receipt_id=uuid4(),
        generated_at=datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC),
    )


class TaskResultRecordMetadataTests(unittest.TestCase):
    """离线：字段矩阵投影、外键闭合与裸延迟外键与 ORM 元数据一致。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.table = Base.metadata.tables["workflow.task_result"]

    def test_column_order_matches_projection(self) -> None:
        self.assertEqual(list(self.table.c.keys()), PROJECTION)

    def test_deferred_bare_uuid_columns_have_no_foreign_key(self) -> None:
        for column in DEFERRED_BARE:
            with self.subTest(column=column):
                col = self.table.c[column]
                self.assertIsInstance(col.type, PGUUID)
                self.assertFalse(col.foreign_keys)

    def test_real_foreign_keys_target_frozen_tables(self) -> None:
        for column, target_table in REAL_FK_TARGETS.items():
            with self.subTest(column=column):
                fks = list(self.table.c[column].foreign_keys)
                self.assertEqual(len(fks), 1)
                self.assertEqual(fks[0].column.table.name, target_table)

    def test_primary_key_is_task_version(self) -> None:
        pk_columns = [column.name for column in self.table.primary_key.columns]
        self.assertEqual(pk_columns, ["task_id", "result_version"])


class TaskResultCommitMappingTests(unittest.TestCase):
    """离线：`record_from_commit` 把域写模型确定性压平为行。"""

    def test_record_from_commit_flattens_fields(self) -> None:
        commit = _commit(task_id=uuid4(), artifact_ids=_fresh_artifact_ids())
        record = record_from_commit(commit)

        self.assertEqual(record.contract_version, "task_result_v1")
        self.assertEqual(record.answer_status, "COMPLETE")
        self.assertEqual(record.citation_count, 3)
        self.assertEqual(record.result_artifact_count, 2)
        self.assertEqual(record.degradation_flags, ["STALE_INPUT"])
        self.assertEqual(record.result_commit_digest, "c" * 64)
        self.assertEqual(record.row_version, 1)

    def test_record_from_commit_serializes_criterion_and_constraint(self) -> None:
        commit = _commit(task_id=uuid4(), artifact_ids=_fresh_artifact_ids())
        record = record_from_commit(commit)

        check = commit.criterion_checks[0]
        self.assertEqual(
            record.criterion_checks,
            [
                {
                    "criterion_id": str(check.criterion_id),
                    "completion_role": "CORE_DELIVERABLE",
                    "status": "SATISFIED",
                    "display_summary": "答复满足核心交付物",
                    "citation_ids": ["c_001"],
                }
            ],
        )
        constraint = commit.constraint_checks[0]
        self.assertEqual(
            record.constraint_checks,
            [{"constraint_id": str(constraint.constraint_id), "status": "SATISFIED"}],
        )


class TaskResultCommitContractTests(unittest.IsolatedAsyncioTestCase):
    """离线：首版之外 result_version 以稳定契约错误失败关闭，不触及持久化。"""

    async def test_first_version_only(self) -> None:
        commit = _commit(task_id=uuid4(), result_version=2, artifact_ids=_fresh_artifact_ids())
        with self.assertRaisesRegex(
            TaskResultCommitContractError, "TASK_RESULT_FIRST_VERSION_ONLY"
        ):
            await TaskResultRepository(session=object()).insert_committed(commit)


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
    """落入 task_result 已提交的依赖链：org/user/7 artifacts/conversation/message/input/task。"""

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
        for artifact_name in (
            "query",
            "answer",
            "envelope",
            "candidate",
            "validation",
            "evaluation_set",
            "gate_decision",
        ):
            artifact_id = ids[artifact_name]
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
                "temporal_workflow_id,last_event_sequence,budget,budget_used,deadline_at,version) "
                "VALUES (:id,:org,:owner,:conversation,:query,:input,:input,:auth,'PLANNING',"
                ":temporal,0,'{}'::jsonb,'{}'::jsonb,:deadline,1)"
            ),
            {
                "id": ids["task"],
                "org": ids["org"],
                "owner": ids["user"],
                "conversation": ids["conversation"],
                "query": ids["query"],
                "input": ids["input"],
                "auth": uuid4(),
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
                "artifact": ids["query"],
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
                "query": ids["query"],
                "owner": ids["user"],
                "org": ids["org"],
                "conversation": ids["conversation"],
                "digest": DIGEST,
            },
        )


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class TaskResultPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev06_task_result_{os.getpid()}_{uuid4().hex[:8]}"
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
                "query",
                "answer",
                "envelope",
                "candidate",
                "validation",
                "evaluation_set",
                "gate_decision",
                "conversation",
                "task",
                "message",
                "input",
            )
        }
        await _seed(self.engine, self.ids)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        assert TEST_DATABASE_URL is not None
        with psycopg.connect(_pg_url(TEST_DATABASE_URL), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)')

    async def _insert(self, commit: TaskResultCommitV1):
        async with self.session_factory() as session:
            result = await TaskResultRepository(session).insert_committed(commit)
            await session.commit()
        return result

    def _artifact_ids(self) -> dict[str, UUID]:
        return {
            "answer": self.ids["answer"],
            "envelope": self.ids["envelope"],
            "candidate": self.ids["candidate"],
            "validation": self.ids["validation"],
            "evaluation_set": self.ids["evaluation_set"],
            "gate_decision": self.ids["gate_decision"],
        }

    def _result_row(self, task_id: UUID) -> dict:
        with psycopg.connect(self.isolated_pg, row_factory=dict_row) as connection:
            rows = list(
                connection.execute(
                    "SELECT result_commit_digest, answer_status, citation_count, row_version "
                    "FROM workflow.task_result WHERE task_id = %s",
                    (task_id,),
                )
            )
        return rows[0]

    async def test_insert_committed(self) -> None:
        commit = _commit(task_id=self.ids["task"], artifact_ids=self._artifact_ids())
        result = await self._insert(commit)
        self.assertEqual(result.disposition, TaskResultCommitDisposition.INSERTED)
        self.assertEqual(result.row_version, 1)

        row = self._result_row(self.ids["task"])
        self.assertEqual(row["result_commit_digest"], "c" * 64)
        self.assertEqual(row["answer_status"], "COMPLETE")
        self.assertEqual(row["citation_count"], 3)

    async def test_idempotent_replay_same_digest(self) -> None:
        commit = _commit(task_id=self.ids["task"], artifact_ids=self._artifact_ids())
        await self._insert(commit)
        replay = await self._insert(commit)

        self.assertEqual(replay.disposition, TaskResultCommitDisposition.IDEMPOTENT_REPLAY)
        self.assertEqual(self._result_row(self.ids["task"])["result_commit_digest"], "c" * 64)

    async def test_conflict_different_digest_does_not_overwrite(self) -> None:
        await self._insert(_commit(task_id=self.ids["task"], artifact_ids=self._artifact_ids()))
        conflicting = _commit(
            task_id=self.ids["task"],
            artifact_ids=self._artifact_ids(),
            result_commit_digest="d" * 64,
        )
        result = await self._insert(conflicting)

        self.assertEqual(result.disposition, TaskResultCommitDisposition.CONFLICT)
        self.assertEqual(result.result_commit_digest, "c" * 64)
        # 既有结果未被覆盖。
        self.assertEqual(self._result_row(self.ids["task"])["result_commit_digest"], "c" * 64)


if __name__ == "__main__":
    unittest.main()
