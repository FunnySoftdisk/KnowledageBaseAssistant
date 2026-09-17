"""审计链离线验收：规范序列化确定性、Hash链算法与迁移不可变守卫。"""

from __future__ import annotations

import hashlib
import importlib.util
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from knowledge_system.modules.audit.domain.audit_event import AuditEventDraft, AuditResult

MIGRATION = (
    Path(__file__).resolve().parents[1] / "migrations/versions/0005_audit_event_chain.py"
)


def load_migration():
    spec = importlib.util.spec_from_file_location("audit_event_chain_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_draft(**overrides: object) -> AuditEventDraft:
    base: dict[str, object] = dict(
        event_id=uuid4(),
        occurred_at=datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC),
        actor_id=uuid4(),
        actor_role_snapshot=(),
        session_id=None,
        source_ip=None,
        device_id=None,
        action="TASK_CREATE",
        resource_type="TASK",
        resource_id="00000000-0000-0000-0000-000000000001",
        result=AuditResult.SUCCESS.value,
        reason_code=None,
        before_digest=None,
        after_digest=None,
        details_json=None,
        trace_id="a" * 32,
    )
    base.update(overrides)
    return AuditEventDraft(**base)


class AuditCanonicalizationTests(unittest.TestCase):
    def test_canonical_bytes_deterministic(self) -> None:
        a = make_draft()
        b = make_draft(event_id=a.event_id, occurred_at=a.occurred_at, actor_id=a.actor_id)
        self.assertEqual(a.canonical_bytes(), b.canonical_bytes())

    def test_canonical_bytes_change_with_any_field(self) -> None:
        a = make_draft()
        changed = make_draft(
            event_id=a.event_id,
            occurred_at=a.occurred_at,
            actor_id=a.actor_id,
            action="TASK_CANCEL",
        )
        self.assertNotEqual(a.canonical_bytes(), changed.canonical_bytes())

    def test_canonical_payload_normalizes_ids_nulls_and_key_order(self) -> None:
        draft = make_draft(actor_id=None, details_json={"z": 1, "a": None})
        payload = draft.canonical_payload()
        self.assertIsNone(payload["actor_id"])
        self.assertEqual(payload["actor_role_snapshot"], [])
        self.assertEqual(payload["event_id"], str(draft.event_id))
        self.assertEqual(payload["occurred_at"], "2026-09-16T12:00:00+00:00")
        # JCS对键排序：details_json内a在z前。
        self.assertEqual(payload["details_json"], {"a": None, "z": 1})
        self.assertIn(b'"details_json":{"a":null,"z":1}', draft.canonical_bytes())

    def test_hash_chain_links_previous(self) -> None:
        first = make_draft()
        second = make_draft()
        head_1 = hashlib.sha256(b"" + first.canonical_bytes()).hexdigest()
        head_2 = hashlib.sha256(head_1.encode("ascii") + second.canonical_bytes()).hexdigest()
        self.assertRegex(head_1, "^[0-9a-f]{64}$")
        self.assertRegex(head_2, "^[0-9a-f]{64}$")
        self.assertNotEqual(head_1, head_2)


class AuditMigrationGuardTests(unittest.TestCase):
    def test_upgrade_registers_immutability_trigger_and_chain_head(self) -> None:
        migration = load_migration()
        with (
            patch.object(migration.op, "execute") as execute,
            patch.object(migration.op, "f", side_effect=lambda name: name),
            patch.object(migration.op, "create_table"),
            patch.object(migration.op, "create_index"),
        ):
            migration.upgrade()
        statements = [str(call.args[0]) for call in execute.call_args_list]
        self.assertTrue(any("reject_audit_mutation" in s for s in statements))
        self.assertTrue(any("audit_event_immutable" in s for s in statements))
        self.assertTrue(any("audit_chain_head" in s for s in statements))

    def test_downgrade_guards_existing_audit_rows(self) -> None:
        migration = load_migration()
        with (
            patch.object(migration.op, "execute") as execute,
            patch.object(migration.op, "f", side_effect=lambda name: name),
            patch.object(
                migration.op, "drop_table", side_effect=RuntimeError("stop after guard")
            ),
            patch.object(migration.op, "drop_index"),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after guard"):
                migration.downgrade()
        self.assertTrue(
            any(
                "AUDIT_DOWNGRADE_REQUIRES_BACKUP_RESTORE" in str(call.args[0])
                for call in execute.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
