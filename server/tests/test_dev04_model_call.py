"""DEV-04B ModelCall账本离线契约：读模型↔ORM映射与迁移降级守卫。

不依赖真实PostgreSQL，验证Token Accounting的EXACT/ESTIMATED_DEV分支与Usage的
EXACT/ESTIMATED_DEV/UNKNOWN三态在`ModelCallV1`（嵌套值对象）与`ModelCallRecord`（判别列+互斥
分支列）之间的无损互转，以及`insert_accepted`对非ACCEPTED状态的稳定拒绝、`0006`迁移降级守卫。
"""

from __future__ import annotations

import importlib.util
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from knowledge_system.infrastructure.persistence.model_call_repository import (
    ModelCallLedgerRepository,
    record_from_v1,
    v1_from_record,
)
from knowledge_system.modules.model_gateway.domain.call_state import (
    ModelCallState,
    ModelCallStateViolation,
)
from knowledge_system.modules.model_gateway.domain.model_call import DataScope, ModelCallV1
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    EstimatedDevTokenAccountingV1,
    ExactTokenAccountingV1,
)
from knowledge_system.modules.model_gateway.domain.usage import (
    EstimatedModelUsageV1,
    ExactModelUsageV1,
    UnknownModelUsageV1,
)

MIGRATION = Path(__file__).resolve().parents[1] / "migrations/versions/0006_model_call_ledger.py"

EXACT_ACCOUNTING = ExactTokenAccountingV1(
    mode="EXACT",
    estimated=False,
    exact_input_tokens=128,
    tokenizer_revision="tok-1",
    chat_template_hash="1" * 64,
)
ESTIMATED_ACCOUNTING = EstimatedDevTokenAccountingV1(
    mode="ESTIMATED_DEV",
    estimated=True,
    estimated_input_tokens=256,
    estimator_revision="est-1",
    serialized_request_bytes=4096,
    protocol_overhead_tokens=32,
)
UNKNOWN_USAGE = UnknownModelUsageV1(kind="UNKNOWN")
EXACT_USAGE = ExactModelUsageV1(kind="EXACT", input_tokens=100, output_tokens=50, source="PROVIDER")
ESTIMATED_USAGE = EstimatedModelUsageV1(
    kind="ESTIMATED_DEV",
    estimated_input_tokens=200,
    estimated_output_tokens=100,
    source="WORST_CASE_RESERVATION",
)


def make_call(*, token_accounting=None, usage=None, status=ModelCallState.ACCEPTED, **overrides):
    return ModelCallV1(
        model_call_id=overrides.pop("model_call_id", uuid4()),
        capability_alias="planning_strong",
        approved_model_id="qwen-plus",
        approved_model_revision="rev1",
        prompt_version="v1",
        context_pack_id=uuid4(),
        context_policy_version="cp-v1",
        authorization_version=3,
        runtime_version="rt-1",
        agent_manifest_version="am-1",
        conversation_memory_version=0,
        user_private_memory_version=0,
        task_memory_version=0,
        token_accounting=token_accounting or EXACT_ACCOUNTING,
        data_scope=DataScope.INTERNAL_PRODUCTION,
        request_hash="a" * 64,
        usage=usage or UNKNOWN_USAGE,
        status=status,
        created_at=datetime.now(UTC),
        **overrides,
    )


class ModelCallRoundtripTests(unittest.TestCase):
    def test_roundtrip_all_accounting_usage_branches(self) -> None:
        accountings = {
            "exact": EXACT_ACCOUNTING,
            "estimated": ESTIMATED_ACCOUNTING,
        }
        usages = {
            "unknown": UNKNOWN_USAGE,
            "exact": EXACT_USAGE,
            "estimated": ESTIMATED_USAGE,
        }
        for accounting_name, accounting in accountings.items():
            for usage_name, usage in usages.items():
                with self.subTest(accounting=accounting_name, usage=usage_name):
                    call = make_call(token_accounting=accounting, usage=usage)
                    restored = v1_from_record(record_from_v1(call))
                    self.assertEqual(restored, call)

    def test_record_flattens_discriminators(self) -> None:
        record = record_from_v1(
            make_call(token_accounting=EXACT_ACCOUNTING, usage=UNKNOWN_USAGE)
        )
        self.assertEqual(record.token_accounting_kind, "EXACT")
        self.assertEqual(record.usage_source, "UNKNOWN")
        self.assertEqual(record.exact_input_tokens, 128)
        self.assertIsNone(record.estimated_input_tokens)
        self.assertIsNone(record.input_tokens)
        self.assertIsNone(record.output_tokens)
        self.assertEqual(record.row_version, 1)
        self.assertEqual(record.status, "ACCEPTED")

        record = record_from_v1(
            make_call(token_accounting=ESTIMATED_ACCOUNTING, usage=ESTIMATED_USAGE)
        )
        self.assertEqual(record.token_accounting_kind, "ESTIMATED_DEV")
        self.assertEqual(record.usage_source, "WORST_CASE_RESERVATION")
        self.assertIsNone(record.exact_input_tokens)
        self.assertEqual(record.estimated_input_tokens, 256)
        self.assertEqual(record.input_tokens, 200)
        self.assertEqual(record.output_tokens, 100)

    def test_estimated_usage_maps_worst_case_reservation_columns(self) -> None:
        record = record_from_v1(
            make_call(token_accounting=EXACT_ACCOUNTING, usage=ESTIMATED_USAGE)
        )
        self.assertEqual(record.usage_source, "WORST_CASE_RESERVATION")
        self.assertEqual(record.input_tokens, 200)
        self.assertEqual(record.output_tokens, 100)
        restored = v1_from_record(record)
        self.assertIsInstance(restored.usage, EstimatedModelUsageV1)
        assert isinstance(restored.usage, EstimatedModelUsageV1)
        self.assertEqual(restored.usage.estimated_input_tokens, 200)
        self.assertEqual(restored.usage.estimated_output_tokens, 100)

    def test_provider_usage_reconstructs_exact(self) -> None:
        record = record_from_v1(make_call(usage=EXACT_USAGE))
        restored = v1_from_record(record)
        self.assertIsInstance(restored.usage, ExactModelUsageV1)
        assert isinstance(restored.usage, ExactModelUsageV1)
        self.assertEqual(restored.usage.input_tokens, 100)
        self.assertEqual(restored.usage.output_tokens, 50)
        self.assertEqual(restored.usage.source, "PROVIDER")


class ModelCallInsertGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_insert_rejects_non_accepted_status(self) -> None:
        repository = ModelCallLedgerRepository(MagicMock())  # type: ignore[arg-type]
        with self.assertRaises(ModelCallStateViolation) as raised:
            await repository.insert_accepted(make_call(status=ModelCallState.SENDING))
        self.assertEqual(raised.exception.code, "MODEL_CALL_INSERT_NOT_ACCEPTED")


class ModelCallMigrationGuardTests(unittest.TestCase):
    def test_downgrade_guard_precedes_drop(self) -> None:
        spec = importlib.util.spec_from_file_location("model_call_migration_guard", MIGRATION)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with (
            patch.object(module.op, "execute") as execute,
            patch.object(module.op, "drop_index", side_effect=RuntimeError("stop after guard")),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after guard"):
                module.downgrade()
        assert "MODEL_CALL_DOWNGRADE_REQUIRES_BACKUP_RESTORE" in str(execute.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
