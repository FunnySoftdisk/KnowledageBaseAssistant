"""0003迁移在无法无损转换的旧数据与降级数据前失败关闭。"""

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations/versions/0f120b8fd5b6_attempt_runtime_dependencies.py"
)


def load_migration():
    spec = importlib.util.spec_from_file_location("attempt_runtime_migration_guard", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AttemptMigrationGuardTests(unittest.TestCase):
    def test_upgrade_guards_legacy_goal_before_creating_runtime_tables(self) -> None:
        migration = load_migration()
        with (
            patch.object(migration.op, "execute") as execute,
            patch.object(migration.op, "f", side_effect=lambda name: name),
            patch.object(
                migration.op, "create_table", side_effect=RuntimeError("stop after guard")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after guard"):
                migration.upgrade()
        assert "ATTEMPT_RUNTIME_UPGRADE_LEGACY_GOAL_PLAN_REQUIRES_RECONCILIATION" in str(
            execute.call_args.args[0]
        )

    def test_downgrade_guards_runtime_data_before_dropping_tables(self) -> None:
        migration = load_migration()
        with (
            patch.object(migration.op, "execute") as execute,
            patch.object(migration.op, "f", side_effect=lambda name: name),
            patch.object(
                migration.op, "drop_constraint", side_effect=RuntimeError("stop after guard")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after guard"):
                migration.downgrade()
        assert "ATTEMPT_RUNTIME_DOWNGRADE_REQUIRES_BACKUP_RESTORE" in str(execute.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
