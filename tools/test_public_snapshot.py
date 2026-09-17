import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from build_public_snapshot import (
    PROJECT_ROOT,
    PublicExportError,
    build_snapshot,
    destination_for,
    scan_file,
)


class PublicSnapshotTests(unittest.TestCase):
    def test_allowlist_and_remapping(self) -> None:
        self.assertIsNone(destination_for("public/README.md"))
        self.assertIsNone(destination_for("README.md"))
        self.assertIsNone(destination_for("contracts/core/README.md"))
        self.assertIsNone(destination_for("server/migrations/README.md"))
        self.assertEqual(
            destination_for("server/src/knowledge_system/__init__.py"),
            "server/src/knowledge_system/__init__.py",
        )
        self.assertEqual(
            destination_for("contracts/core/manifest.json"),
            "contracts/core/manifest.json",
        )
        for private_path in (
            "docs/开发状态.md",
            "deploy/m0/bailian-account.json",
            "模块详细设计/08-智能任务与编排模块详细设计.md",
            "团队知识助手产品需求文档.md",
            "contracts/prompts/external-research-final-system-v1.txt",
        ):
            with self.subTest(private_path=private_path):
                self.assertIsNone(destination_for(private_path))

    def test_snapshot_contains_only_allowlisted_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "snapshot"
            report = build_snapshot(output)
            self.assertEqual(report["status"], "PASS")
            self.assertFalse((output / "README.md").exists())
            self.assertFalse((output / "contracts/core/README.md").exists())
            self.assertFalse((output / "server/migrations/README.md").exists())
            self.assertTrue((output / "PUBLIC_EXPORT.md").is_file())
            self.assertTrue((output / "server/pyproject.toml").is_file())
            self.assertTrue((output / "server/alembic.ini").is_file())
            self.assertTrue((output / "server/migrations/env.py").is_file())
            self.assertTrue(
                (
                    output / "server/migrations/versions/0002_goal_plan_persistence.py"
                ).is_file()
            )
            self.assertTrue(
                (
                    output
                    / "server/migrations/versions/0004_goal_clarification_request.py"
                ).is_file()
            )
            self.assertTrue((output / "contracts/core/manifest.json").is_file())
            self.assertTrue((output / "tools/reconcile_legacy_goal_plan.py").is_file())
            self.assertTrue((output / "tools/test_reconcile_legacy_goal_plan.py").is_file())
            self.assertFalse((output / "docs").exists())
            self.assertFalse((output / "deploy").exists())
            self.assertFalse((output / ".git").exists())

    def test_nonempty_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "snapshot"
            output.mkdir()
            (output / "existing").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(
                PublicExportError, "OUTPUT_MUST_BE_ABSENT_OR_EMPTY_DIRECTORY"
            ):
                build_snapshot(output)

    def test_output_inside_private_project_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            PublicExportError, "OUTPUT_INSIDE_PRIVATE_PROJECT_FORBIDDEN"
        ):
            build_snapshot(PROJECT_ROOT / ".public-snapshot-test")

    def test_secret_markers_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "candidate.txt"
            for marker in (
                "github_pat_" + "A" * 24,
                "ghp_" + "B" * 24,
                "sk-" + "C" * 20,
                "BEGIN " + "OPENSSH PRIVATE KEY",
            ):
                with self.subTest(marker=marker[:8]):
                    path.write_text(marker, encoding="utf-8")
                    with self.assertRaises(PublicExportError):
                        scan_file(path)

    def test_untracked_allowlisted_path_is_not_exported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "snapshot"
            with patch(
                "build_public_snapshot.tracked_files",
                return_value=["server/pyproject.toml"],
            ):
                report = build_snapshot(output)
            self.assertEqual(report["file_count"], 1)
            self.assertFalse((output / "README.md").exists())


if __name__ == "__main__":
    unittest.main()
