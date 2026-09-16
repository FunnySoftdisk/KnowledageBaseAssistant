"""Goal/Plan ORM与批准字段投影的一致性测试。"""

import json
import unittest
from pathlib import Path

from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from knowledge_system.infrastructure.persistence import Base, planning_models  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
FIELD_MATRIX = ROOT / "contracts" / "core" / "core-field-matrix-v1.json"

PLAN_TABLES = {
    "workflow.task_plan_version",
    "workflow.task_plan_item_definition",
    "workflow.task_plan_hypothesis",
    "workflow.task_compiled_criterion",
    "workflow.plan_item_runtime",
}

GOAL_COLUMNS = [
    "understanding_id",
    "task_id",
    "input_id",
    "task_attempt_id",
    "contract_version",
    "prompt_profile",
    "prompt_profile_digest",
    "model_alias",
    "goal_context_artifact_id",
    "context_digest",
    "raw_model_artifact_id",
    "goal_artifact_id",
    "understanding_digest",
    "gate_profile",
    "gate_profile_digest",
    "gate_artifact_id",
    "gate_digest",
    "gate_outcome",
    "subgoal_count",
    "criterion_count",
    "assumption_count",
    "blocking_issue_count",
    "created_at",
]


class PlanningPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = json.loads(FIELD_MATRIX.read_text(encoding="utf-8"))

    def test_plan_tables_have_exact_approved_columns(self) -> None:
        projections = {
            **self.matrix["closed_table_projections"],
            **self.matrix["accepted_dependency_table_projections_pending_orm"],
        }
        for table_name in sorted(PLAN_TABLES):
            with self.subTest(table=table_name):
                self.assertEqual(
                    list(Base.metadata.tables[table_name].c.keys()),
                    projections[table_name],
                )

    def test_goal_table_has_no_duplicated_goal_text(self) -> None:
        goal = Base.metadata.tables["workflow.task_goal_understanding"]
        self.assertEqual(list(goal.c.keys()), GOAL_COLUMNS)
        for forbidden in self.matrix["column_rules"]["workflow.task_goal_understanding"][
            "forbidden"
        ]:
            self.assertNotIn(forbidden, goal.c)

    def test_runtime_references_immutable_plan_item_composite_key(self) -> None:
        runtime = Base.metadata.tables["workflow.plan_item_runtime"]
        foreign_key = next(
            constraint
            for constraint in runtime.foreign_key_constraints
            if len(constraint.elements) == 3
        )
        self.assertEqual(
            [element.parent.name for element in foreign_key.elements],
            ["task_id", "plan_version", "plan_item_id"],
        )
        self.assertEqual(
            [element.target_fullname for element in foreign_key.elements],
            [
                "workflow.task_plan_item_definition.task_id",
                "workflow.task_plan_item_definition.plan_version",
                "workflow.task_plan_item_definition.plan_item_id",
            ],
        )

    def test_attempt_runtime_dependencies_have_closed_foreign_keys(self) -> None:
        goal = Base.metadata.tables["workflow.task_goal_understanding"]
        plan = Base.metadata.tables["workflow.task_plan_version"]
        runtime = Base.metadata.tables["workflow.plan_item_runtime"]
        self.assertTrue(goal.c.task_attempt_id.foreign_keys)
        self.assertTrue(plan.c.task_attempt_id.foreign_keys)
        self.assertTrue(plan.c.created_by_agent_instance_id.foreign_keys)
        self.assertTrue(runtime.c.active_task_attempt_id.foreign_keys)

    def test_ready_goal_may_retain_model_issues_rejected_by_gate(self) -> None:
        goal = Base.metadata.tables["workflow.task_goal_understanding"]
        constraint = next(
            item
            for item in goal.constraints
            if isinstance(item, CheckConstraint)
            and item.name == "ck_task_goal_understanding_gate_blocking_issue_shape"
        )
        expression = str(constraint.sqltext)
        self.assertIn("gate_outcome <> 'REQUEST_USER_INPUT'", expression)
        self.assertNotIn("READY_TO_PLAN' AND blocking_issue_count = 0", expression)

    def test_every_goal_plan_table_compiles_for_postgresql(self) -> None:
        dialect = postgresql.dialect()
        for table_name in sorted({*PLAN_TABLES, "workflow.task_goal_understanding"}):
            with self.subTest(table=table_name):
                ddl = str(CreateTable(Base.metadata.tables[table_name]).compile(dialect=dialect))
                self.assertIn("CREATE TABLE", ddl)


if __name__ == "__main__":
    unittest.main()
