"""旧0002 Goal/Plan对账工具的纯逻辑单元测试（无数据库依赖）。"""

import hashlib
import json
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from reconcile_legacy_goal_plan import (
    DELETE_ORDER,
    GOAL_TABLE,
    PLAN_CHILD_TABLES,
    PLAN_TABLE,
    archive_manifest,
    build_legacy_predicates,
    delete_statement,
    select_statement,
    serialize_value,
)

UUID_STR = "12345678-1234-5678-1234-567812345678"


class LegacyPredicateTests(unittest.TestCase):
    def test_no_attempt_table_treats_all_rows_as_legacy(self) -> None:
        predicates = build_legacy_predicates(task_attempt_exists=False)
        self.assertFalse(predicates.task_attempt_exists)
        self.assertEqual(predicates.goal, "TRUE")
        self.assertEqual(predicates.plan, "TRUE")

    def test_with_attempt_table_uses_dangling_reference(self) -> None:
        predicates = build_legacy_predicates(task_attempt_exists=True)
        dangling = "task_attempt_id NOT IN (SELECT id FROM workflow.task_attempt)"
        self.assertEqual(predicates.goal, dangling)
        self.assertEqual(predicates.plan, dangling)


class DeleteStatementTests(unittest.TestCase):
    def test_delete_order_is_children_before_parents(self) -> None:
        self.assertEqual(
            DELETE_ORDER,
            (
                "workflow.plan_item_runtime",
                "workflow.task_compiled_criterion",
                "workflow.task_plan_hypothesis",
                "workflow.task_plan_item_definition",
                PLAN_TABLE,
                GOAL_TABLE,
            ),
        )
        for child in PLAN_CHILD_TABLES:
            self.assertLess(DELETE_ORDER.index(child), DELETE_ORDER.index(PLAN_TABLE))
        self.assertLess(DELETE_ORDER.index(PLAN_TABLE), DELETE_ORDER.index(GOAL_TABLE))

    def test_plan_child_delete_joins_legacy_plan_scope(self) -> None:
        predicates = build_legacy_predicates(task_attempt_exists=False)
        statement = delete_statement("workflow.task_plan_item_definition", predicates)
        self.assertIn("(task_id, plan_version) IN", statement)
        self.assertIn(f"SELECT task_id, plan_version FROM {PLAN_TABLE}", statement)
        self.assertIn("TRUE", statement)

    def test_goal_and_plan_delete_statements(self) -> None:
        predicates = build_legacy_predicates(task_attempt_exists=False)
        self.assertEqual(
            delete_statement(PLAN_TABLE, predicates), f"DELETE FROM {PLAN_TABLE} WHERE TRUE"
        )
        self.assertEqual(
            delete_statement(GOAL_TABLE, predicates), f"DELETE FROM {GOAL_TABLE} WHERE TRUE"
        )

    def test_select_matches_delete_scope(self) -> None:
        predicates = build_legacy_predicates(task_attempt_exists=True)
        for table in (*PLAN_CHILD_TABLES, PLAN_TABLE, GOAL_TABLE):
            with self.subTest(table=table):
                delete_sql = delete_statement(table, predicates)
                select_sql = select_statement(table, predicates)
                self.assertEqual(delete_sql, select_sql.replace("SELECT * FROM", "DELETE FROM", 1))

    def test_unknown_table_is_rejected(self) -> None:
        predicates = build_legacy_predicates(task_attempt_exists=False)
        with self.assertRaises(ValueError):
            delete_statement("workflow.unknown_table", predicates)


class SerializationTests(unittest.TestCase):
    def test_native_python_types_survive(self) -> None:
        self.assertEqual(serialize_value("text"), "text")
        self.assertEqual(serialize_value(42), 42)
        self.assertEqual(serialize_value(1.5), 1.5)
        self.assertEqual(serialize_value(True), True)
        self.assertIsNone(serialize_value(None))

    def test_uuid_datetime_decimal_bytes_are_stringified(self) -> None:
        self.assertEqual(serialize_value(UUID(UUID_STR)), UUID_STR)
        stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        self.assertEqual(serialize_value(stamp), "2026-01-02T03:04:05+00:00")
        self.assertEqual(serialize_value(date(2026, 1, 2)), "2026-01-02")
        self.assertEqual(serialize_value(Decimal("1.50")), "1.50")
        self.assertEqual(serialize_value(b"\x00\xff"), "00ff")

    def test_nested_containers_are_recursively_serialized(self) -> None:
        value = {"k": [UUID(UUID_STR), {"n": date(2026, 1, 2)}]}
        self.assertEqual(
            serialize_value(value),
            {"k": [UUID_STR, {"n": "2026-01-02"}]},
        )


class ArchiveManifestTests(unittest.TestCase):
    def test_manifest_digest_is_deterministic_and_correct(self) -> None:
        archived = {
            GOAL_TABLE: [{"understanding_id": UUID_STR, "count": 2}],
            PLAN_TABLE: [{"task_id": UUID_STR, "plan_version": 1}],
        }
        created = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
        manifest = archive_manifest(archived, created)
        self.assertEqual(manifest["tool"], "reconcile_legacy_goal_plan")
        self.assertEqual(manifest["created_at"], "2026-09-16T12:00:00+00:00")
        self.assertEqual(manifest["tables"][GOAL_TABLE]["row_count"], 1)
        self.assertEqual(manifest["tables"][GOAL_TABLE]["file"], "task_goal_understanding.json")

        body = json.dumps(archived[GOAL_TABLE], ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.assertEqual(
            manifest["tables"][GOAL_TABLE]["sha256"],
            hashlib.sha256(body).hexdigest(),
        )

    def test_manifest_is_identical_for_reordered_input(self) -> None:
        created = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
        first = archive_manifest({GOAL_TABLE: [{"a": 1}]}, created)
        second = archive_manifest({GOAL_TABLE: [{"a": 1}]}, created)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
