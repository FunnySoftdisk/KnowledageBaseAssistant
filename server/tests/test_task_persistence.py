"""DEV-01任务入口事务ORM与批准字段矩阵的一致性测试。"""

import json
import unittest
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from knowledge_system.infrastructure.persistence import (  # noqa: F401
    Base,
    foundation_models,
    task_models,
)

ROOT = Path(__file__).resolve().parents[2]
FIELD_MATRIX = ROOT / "contracts" / "core" / "core-field-matrix-v1.json"

TASK_ENTRY_TABLES = {
    "workflow.conversation_message",
    "workflow.intelligent_task",
    "workflow.task_input_snapshot",
    "workflow.task_attachment_binding",
    "workflow.task_explicit_constraint",
    "workflow.task_intent_parse",
    "workflow.fixed_workflow_admission",
    "workflow.task_event",
    "integration.outbox_message",
    "integration.idempotency_record",
}


class TaskPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = json.loads(FIELD_MATRIX.read_text(encoding="utf-8"))

    def test_task_entry_tables_have_exact_approved_columns(self) -> None:
        projections = self.matrix["accepted_dependency_table_projections_pending_orm"]
        projections = {
            **projections,
            "workflow.task_input_snapshot": self.matrix["closed_table_projections"][
                "workflow.task_input_snapshot"
            ],
        }
        for table_name in sorted(TASK_ENTRY_TABLES):
            with self.subTest(table=table_name):
                table = Base.metadata.tables[table_name]
                self.assertEqual(list(table.c.keys()), projections[table_name])

    def test_task_does_not_restore_forbidden_input_copies(self) -> None:
        task = Base.metadata.tables["workflow.intelligent_task"]
        rules = self.matrix["column_rules"]["workflow.intelligent_task"]
        for column in rules["required"]:
            self.assertIn(column, task.c)
            self.assertFalse(task.c[column].nullable)
        for column in rules["forbidden"]:
            self.assertNotIn(column, task.c)
        self.assertEqual(task.c.status.server_default.arg, "QUEUED")

    def test_circular_task_input_foreign_keys_are_initially_deferred(self) -> None:
        task = Base.metadata.tables["workflow.intelligent_task"]
        for column_name in ("initial_input_id", "current_input_id"):
            foreign_key = next(iter(task.c[column_name].foreign_keys))
            self.assertTrue(foreign_key.use_alter)
            self.assertTrue(foreign_key.deferrable)
            self.assertEqual(foreign_key.initially, "DEFERRED")

    def test_unapproved_dependencies_are_explicitly_fk_deferred(self) -> None:
        task = Base.metadata.tables["workflow.intelligent_task"]
        binding = Base.metadata.tables["workflow.task_attachment_binding"]
        self.assertFalse(task.c.authorization_ref_id.foreign_keys)
        self.assertFalse(binding.c.preflight_run_id.foreign_keys)

    def test_postgresql_ddl_compiles_for_every_task_entry_table(self) -> None:
        dialect = postgresql.dialect()
        for table_name in sorted(TASK_ENTRY_TABLES):
            with self.subTest(table=table_name):
                ddl = str(CreateTable(Base.metadata.tables[table_name]).compile(dialect=dialect))
                self.assertIn("CREATE TABLE", ddl)
                self.assertIn(Base.metadata.tables[table_name].name, ddl)

    def test_sensitive_or_duplicated_payload_columns_are_absent(self) -> None:
        message = Base.metadata.tables["workflow.conversation_message"]
        idempotency = Base.metadata.tables["integration.idempotency_record"]
        parse = Base.metadata.tables["workflow.task_intent_parse"]
        self.assertNotIn("content_preview", message.c)
        self.assertNotIn("idempotency_key", idempotency.c)
        self.assertNotIn("confidence", parse.c)

    def test_attachment_binding_uses_frozen_lifecycle_states(self) -> None:
        binding = Base.metadata.tables["workflow.task_attachment_binding"]
        state_constraint = next(
            constraint
            for constraint in binding.constraints
            if constraint.name == "ck_task_attachment_binding_state_allowed"
        )
        self.assertIn("'DELETED'", str(state_constraint.sqltext))
        self.assertNotIn("'RELEASED'", str(state_constraint.sqltext))


if __name__ == "__main__":
    unittest.main()
