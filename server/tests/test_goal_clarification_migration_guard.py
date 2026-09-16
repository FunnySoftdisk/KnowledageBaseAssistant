"""0004迁移不得通过降级静默删除用户输入请求。"""

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

MIGRATION = (
    Path(__file__).resolve().parents[1] / "migrations/versions/0004_goal_clarification_request.py"
)


def load_migration():
    spec = importlib.util.spec_from_file_location("goal_clarification_migration_guard", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GoalClarificationMigrationGuardTests(unittest.TestCase):
    def test_downgrade_guard_precedes_drop(self) -> None:
        migration = load_migration()
        with (
            patch.object(migration.op, "execute") as execute,
            patch.object(migration.op, "drop_index", side_effect=RuntimeError("stop after guard")),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after guard"):
                migration.downgrade()
        assert "GOAL_CLARIFICATION_DOWNGRADE_REQUIRES_BACKUP_RESTORE" in str(
            execute.call_args.args[0]
        )


if __name__ == "__main__":
    unittest.main()
