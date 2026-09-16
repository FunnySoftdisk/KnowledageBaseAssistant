"""DEV-01C Attempt运行依赖表与批准字段矩阵的一致性测试。"""

import json
import unittest
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from knowledge_system.infrastructure.persistence import (  # noqa: F401
    Base,
    attempt_models,
)

ROOT = Path(__file__).resolve().parents[2]
FIELD_MATRIX = ROOT / "contracts" / "core" / "core-field-matrix-v1.json"

ATTEMPT_TABLES = {
    "workflow.agent_instance",
    "workflow.task_attempt",
    "workflow.context_pack",
    "workflow.context_pack_item",
}

# 字段契约未冻结/复合键无法单列引用，保持裸UUID的列。
DEFERRED_BARE_COLUMNS = {
    "workflow.task_attempt": {
        "plan_item_id",
        "action_run_group_id",
        "capability_binding_snapshot_id",
        "budget_reservation_id",
        "replan_admission_id",
        "final_synthesis_snapshot_id",
        "runtime_decision_id",
    },
    "workflow.context_pack": {
        "action_attempt_id",
        "authorization_ref_id",
    },
}

# DEV-01闭合的Attempt/Agent外键。
CLOSED_FOREIGN_KEYS = {
    "workflow.task_goal_understanding": {"task_attempt_id"},
    "workflow.task_plan_version": {"task_attempt_id", "created_by_agent_instance_id"},
    "workflow.plan_item_runtime": {"active_task_attempt_id"},
    "workflow.task_attempt": {
        "task_id",
        "input_id",
        "conversation_id",
        "agent_instance_id",
        "context_pack_id",
        "resumes_task_attempt_id",
    },
    "workflow.agent_instance": {"task_id", "parent_agent_instance_id"},
    "workflow.context_pack": {
        "task_id",
        "created_by_attempt_id",
        "conversation_id",
        "actor_id",
        "body_artifact_id",
    },
    "workflow.context_pack_item": {"context_pack_id"},
}


class AttemptPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = json.loads(FIELD_MATRIX.read_text(encoding="utf-8"))

    def test_attempt_tables_have_exact_approved_columns(self) -> None:
        projections = self.matrix["closed_table_projections"]
        for table_name in sorted(ATTEMPT_TABLES):
            with self.subTest(table=table_name):
                self.assertEqual(
                    list(Base.metadata.tables[table_name].c.keys()),
                    projections[table_name],
                )

    def test_deferred_bare_uuid_columns_have_no_foreign_key(self) -> None:
        for table_name, columns in DEFERRED_BARE_COLUMNS.items():
            table = Base.metadata.tables[table_name]
            for column_name in columns:
                with self.subTest(table=table_name, column=column_name):
                    self.assertFalse(table.c[column_name].foreign_keys)

    def test_dev01_closed_foreign_keys_are_real(self) -> None:
        for table_name, columns in CLOSED_FOREIGN_KEYS.items():
            table = Base.metadata.tables[table_name]
            for column_name in columns:
                with self.subTest(table=table_name, column=column_name):
                    self.assertTrue(table.c[column_name].foreign_keys)

    def test_context_pack_to_task_attempt_circular_fk_uses_alter(self) -> None:
        pack = Base.metadata.tables["workflow.context_pack"]
        foreign_key = next(iter(pack.c.created_by_attempt_id.foreign_keys))
        self.assertTrue(foreign_key.use_alter)
        self.assertEqual(
            foreign_key.target_fullname, "workflow.task_attempt.id"
        )

    def test_every_attempt_table_compiles_for_postgresql(self) -> None:
        dialect = postgresql.dialect()
        for table_name in sorted(ATTEMPT_TABLES):
            with self.subTest(table=table_name):
                ddl = str(CreateTable(Base.metadata.tables[table_name]).compile(dialect=dialect))
                self.assertIn("CREATE TABLE", ddl)


if __name__ == "__main__":
    unittest.main()
