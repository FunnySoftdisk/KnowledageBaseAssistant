"""DEV-01字段矩阵逐列验收：机器字段矩阵与ORM映射的穷尽双向一致性审计。

审计不是抽查某个子集，而是把``core-field-matrix-v1.json``里声明的每一张表、每一列、
每一条延迟外键与SQLAlchemy元数据做双向比对：

- ``implemented_orm_tables``必须与ORM实际建表集合完全一致；
- 每张已实现表都必须在``closed_table_projections``或
  ``accepted_dependency_table_projections_pending_orm``中有且仅有一份列投影；
- 每份投影的列顺序必须与ORM列顺序逐位一致；
- ``column_rules``的required/forbidden/default必须成立；
- ``deferred_foreign_keys``必须与ORM中“裸UUID且无外键”的待闭合列完全一致。

任何一侧多出或少掉一列都会让本测试失败，从而阻止字段矩阵与实现发生静默漂移。
"""

import json
import unittest
from pathlib import Path

from sqlalchemy.dialects.postgresql import UUID as PGUUID

from knowledge_system.infrastructure.persistence import (  # noqa: F401
    Base,
    attempt_models,
    foundation_models,
    planning_models,
    task_models,
)

ROOT = Path(__file__).resolve().parents[2]
FIELD_MATRIX = ROOT / "contracts" / "core" / "core-field-matrix-v1.json"

# 按设计不构成数据库外键、也不属于延迟外键的多态/不透明UUID列：
# 多态引用由配套的*_type列决定指向，外部系统运行ID不是数据库外键。
POLYMORPHIC_OR_OPAQUE_UUID_COLUMNS = {
    "integration.idempotency_record.resource_id",
    "integration.outbox_message.aggregate_id",
    "workflow.task_event.business_ref_id",
    "workflow.context_pack_item.item_id",
    "workflow.context_pack_item.source_id",
    "workflow.task_attempt.temporal_child_run_id",
    "audit.audit_event.actor_id",
    "audit.audit_event.session_id",
    "audit.audit_event.device_id",
}

# 契约尚未冻结，保持裸UUID、不得伪造关系闭包的全部延迟外键。
CANONICAL_DEFERRED_FOREIGN_KEYS = {
    "workflow.intelligent_task.authorization_ref_id",
    "workflow.task_attachment_binding.preflight_run_id",
    "content.artifact.origin_upload_id",
    "content.artifact.preflight_run_id",
    "workflow.task_attempt.plan_item_id",
    "workflow.task_attempt.action_run_group_id",
    "workflow.task_attempt.capability_binding_snapshot_id",
    "workflow.task_attempt.budget_reservation_id",
    "workflow.task_attempt.replan_admission_id",
    "workflow.task_attempt.final_synthesis_snapshot_id",
    "workflow.task_attempt.runtime_decision_id",
    "workflow.context_pack.action_attempt_id",
    "workflow.context_pack.authorization_ref_id",
    "workflow.user_input_request.resume_bundle_id",
    "workflow.user_input_request.runtime_blocking_issue_id",
}


class FieldMatrixAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = json.loads(FIELD_MATRIX.read_text(encoding="utf-8"))
        cls.projections = {
            **cls.matrix["closed_table_projections"],
            **cls.matrix["accepted_dependency_table_projections_pending_orm"],
        }

    def test_implemented_orm_tables_match_metadata_exactly(self) -> None:
        self.assertEqual(
            set(self.matrix["implemented_orm_tables"]),
            set(Base.metadata.tables.keys()),
        )

    def test_every_implemented_table_has_exactly_one_projection(self) -> None:
        closed = set(self.matrix["closed_table_projections"])
        pending = set(self.matrix["accepted_dependency_table_projections_pending_orm"])
        self.assertTrue(closed.isdisjoint(pending), "投影映射键不得重叠")
        self.assertEqual(
            set(self.projections),
            set(self.matrix["implemented_orm_tables"]),
            "每张已实现表必须有且仅有一份列投影",
        )

    def test_every_projected_column_matches_orm_order_exactly(self) -> None:
        for table_name in sorted(self.projections):
            with self.subTest(table=table_name):
                self.assertIn(table_name, Base.metadata.tables)
                self.assertEqual(
                    list(Base.metadata.tables[table_name].c.keys()),
                    self.projections[table_name],
                )

    def test_column_rules_hold(self) -> None:
        task = Base.metadata.tables["workflow.intelligent_task"]
        task_rules = self.matrix["column_rules"]["workflow.intelligent_task"]
        for column in task_rules["required"]:
            with self.subTest(table="workflow.intelligent_task", column=column):
                self.assertIn(column, task.c)
                self.assertFalse(task.c[column].nullable)
        for column in task_rules["forbidden"]:
            self.assertNotIn(column, task.c)
        self.assertEqual(task.c.status.server_default.arg, task_rules["public_initial_status"])

        goal = Base.metadata.tables["workflow.task_goal_understanding"]
        goal_rules = self.matrix["column_rules"]["workflow.task_goal_understanding"]
        for column in goal_rules["required"]:
            with self.subTest(table="workflow.task_goal_understanding", column=column):
                self.assertIn(column, goal.c)
                self.assertFalse(goal.c[column].nullable)
        for column in goal_rules["forbidden"]:
            self.assertNotIn(column, goal.c)

    def test_deferred_foreign_keys_are_exact_and_bare(self) -> None:
        self.assertEqual(
            set(self.matrix["deferred_foreign_keys"]),
            CANONICAL_DEFERRED_FOREIGN_KEYS,
        )
        for qualified in CANONICAL_DEFERRED_FOREIGN_KEYS:
            table_name, column_name = qualified.rsplit(".", 1)
            with self.subTest(column=qualified):
                column = Base.metadata.tables[table_name].c[column_name]
                self.assertIsInstance(column.type, PGUUID)
                self.assertFalse(column.foreign_keys)

    def test_no_bare_uuid_column_outside_approved_sets_is_undocumented(self) -> None:
        # 除延迟外键外，允许裸UUID的只剩主键、被其它表外键引用的标识列（如
        # task_event.id 被 outbox.event_id 引用）与按设计多态/不透明引用；逐一核对这些
        # 列确实不属于“待闭合延迟外键”，防止未来新增裸UUID列被静默漏报。
        referenced_targets = {
            foreign_key.column
            for table in Base.metadata.tables.values()
            for foreign_key in table.foreign_keys
        }
        approved_bare = (
            set(self.matrix["deferred_foreign_keys"]) | POLYMORPHIC_OR_OPAQUE_UUID_COLUMNS
        )
        for table_name, table in Base.metadata.tables.items():
            for column in table.c:
                if not isinstance(column.type, PGUUID) or column.foreign_keys:
                    continue
                if column.primary_key or column in referenced_targets:
                    continue
                qualified = f"{table_name}.{column.name}"
                self.assertIn(
                    qualified,
                    approved_bare,
                    f"裸UUID列{qualified}未在deferred_foreign_keys或多态引用中登记",
                )


if __name__ == "__main__":
    unittest.main()
