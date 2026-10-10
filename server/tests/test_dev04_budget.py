"""DEV-04C 预算预留与账本离线契约：读模型↔ORM映射、结算分账守恒与迁移降级守卫。

不依赖真实PostgreSQL，验证`BudgetReservationV1`/`BudgetLedgerEntryV1`（嵌套五维量值对象）与
`BudgetReservationRecord`/`BudgetLedgerEntryRecord`（扁平预留/增量列）之间的无损互转，结算分账
`settle_deltas`按构造保证每维预留=实耗+退还、超额拒绝，预留状态机非法转移稳定失败，`insert_reserved`
对非RESERVED状态与不一致RESERVE分录的稳定拒绝，以及`0007`迁移降级守卫。
"""

from __future__ import annotations

import importlib.util
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from knowledge_system.infrastructure.persistence.budget_repository import (
    BudgetLedgerContractError,
    BudgetLedgerRepository,
    entry_record_from_v1,
    entry_v1_from_record,
    reservation_record_from_v1,
    reservation_v1_from_record,
)
from knowledge_system.modules.model_gateway.domain.budget_ledger import (
    BudgetLedgerEntryKind,
    BudgetLedgerEntryV1,
    BudgetSettlementOverrunError,
    is_conserved,
    settle_deltas,
)
from knowledge_system.modules.model_gateway.domain.budget_quantity import (
    BudgetQuantityDeltaV1,
    BudgetQuantityV1,
)
from knowledge_system.modules.model_gateway.domain.budget_reservation import (
    BudgetReservationStateViolation,
    BudgetReservationStatus,
    BudgetReservationV1,
    can_transition,
    resolve_settlement,
)
from knowledge_system.modules.model_gateway.domain.usage import (
    EstimatedModelUsageV1,
    ExactModelUsageV1,
    UnknownModelUsageV1,
)

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations/versions/0007_budget_reservation_ledger.py"
)

DIGEST = "a" * 64
RESERVED = BudgetQuantityV1(
    model_call_count=1,
    input_tokens=100,
    output_tokens=50,
    tool_count=0,
    cost_amount=Decimal("0.50"),
)


def make_reservation(
    *,
    reserved: BudgetQuantityV1 | None = None,
    settled: BudgetQuantityV1 | None = None,
    status: BudgetReservationStatus = BudgetReservationStatus.RESERVED,
    **overrides,
) -> BudgetReservationV1:
    return BudgetReservationV1(
        reservation_id=overrides.pop("reservation_id", uuid4()),
        task_id=overrides.pop("task_id", uuid4()),
        account_id=overrides.pop("account_id", uuid4()),
        reservation_version=1,
        reserved=reserved if reserved is not None else RESERVED,
        settled=settled if settled is not None else BudgetQuantityV1(),
        reservation_digest=DIGEST,
        status=status,
        created_at=datetime.now(UTC),
        row_version=1,
        **overrides,
    )


def make_entry(
    *,
    reservation_id: object | None = None,
    entry_kind: BudgetLedgerEntryKind = BudgetLedgerEntryKind.RESERVE,
    delta: BudgetQuantityDeltaV1 | None = None,
    **overrides,
) -> BudgetLedgerEntryV1:
    return BudgetLedgerEntryV1(
        entry_id=overrides.pop("entry_id", uuid4()),
        task_id=overrides.pop("task_id", uuid4()),
        account_id=overrides.pop("account_id", uuid4()),
        reservation_id=reservation_id if reservation_id is not None else uuid4(),
        entry_kind=entry_kind,
        delta=delta if delta is not None else BudgetQuantityDeltaV1(),
        created_at=datetime.now(UTC),
        **overrides,
    )


class BudgetRoundtripTests(unittest.TestCase):
    def test_reservation_roundtrip_with_settled(self) -> None:
        reservation = make_reservation(
            settled=BudgetQuantityV1(
                model_call_count=1,
                input_tokens=80,
                output_tokens=40,
                tool_count=0,
                cost_amount=Decimal("0.40"),
            )
        )
        restored = reservation_v1_from_record(reservation_record_from_v1(reservation))
        self.assertEqual(restored, reservation)

    def test_reservation_flattens_quantity_columns(self) -> None:
        record = reservation_record_from_v1(make_reservation())
        self.assertEqual(record.reserved_model_call_count, 1)
        self.assertEqual(record.reserved_input_tokens, 100)
        self.assertEqual(record.reserved_cost_amount, Decimal("0.50"))
        self.assertEqual(record.settled_input_tokens, 0)
        self.assertEqual(record.settled_cost_amount, Decimal("0"))
        self.assertEqual(record.status, "RESERVED")
        self.assertEqual(record.row_version, 1)

    def test_entry_roundtrip_signed_delta(self) -> None:
        entry = make_entry(
            entry_kind=BudgetLedgerEntryKind.RETURN,
            delta=BudgetQuantityDeltaV1(
                model_call_count=0,
                input_tokens=-20,
                output_tokens=-10,
                tool_count=0,
                cost_amount=Decimal("-0.10"),
            ),
        )
        restored = entry_v1_from_record(entry_record_from_v1(entry))
        self.assertEqual(restored, entry)
        self.assertEqual(restored.delta.input_tokens, -20)


class SettleDeltasTests(unittest.TestCase):
    def test_known_settle_returns_difference(self) -> None:
        settled = BudgetQuantityV1(
            model_call_count=1,
            input_tokens=80,
            output_tokens=40,
            tool_count=0,
            cost_amount=Decimal("0.40"),
        )
        settle, ret = settle_deltas(RESERVED, settled)
        self.assertEqual(settle, settled.as_delta())
        self.assertEqual(ret.input_tokens, -20)
        self.assertEqual(ret.output_tokens, -10)
        self.assertEqual(ret.cost_amount, Decimal("-0.10"))
        self.assertTrue(is_conserved(RESERVED, settled, RESERVED.subtract(settled)))

    def test_worst_case_settle_has_zero_return(self) -> None:
        settle, ret = settle_deltas(RESERVED, RESERVED)
        self.assertEqual(settle, RESERVED.as_delta())
        self.assertEqual(ret, BudgetQuantityDeltaV1())
        self.assertTrue(is_conserved(RESERVED, RESERVED, BudgetQuantityV1()))

    def test_overrun_raises(self) -> None:
        overrun = BudgetQuantityV1(
            model_call_count=1,
            input_tokens=101,
            output_tokens=0,
            tool_count=0,
            cost_amount=Decimal("0"),
        )
        with self.assertRaises(BudgetSettlementOverrunError) as raised:
            settle_deltas(RESERVED, overrun)
        self.assertIn("BUDGET_SETTLEMENT_OVERRUN", str(raised.exception))

    def test_conservation_rejects_unbalanced(self) -> None:
        settled = BudgetQuantityV1(model_call_count=1, input_tokens=80)
        returned = BudgetQuantityV1(input_tokens=10)  # 差10 token未退还
        self.assertFalse(is_conserved(RESERVED, settled, returned))


class ReservationStateMachineTests(unittest.TestCase):
    def test_legal_transitions(self) -> None:
        self.assertTrue(
            can_transition(BudgetReservationStatus.RESERVED, BudgetReservationStatus.CONSUMING)
        )
        self.assertTrue(
            can_transition(BudgetReservationStatus.CONSUMING, BudgetReservationStatus.SETTLED)
        )
        self.assertTrue(
            can_transition(
                BudgetReservationStatus.CONSUMING, BudgetReservationStatus.RECONCILING
            )
        )
        self.assertTrue(
            can_transition(BudgetReservationStatus.RESERVED, BudgetReservationStatus.RELEASED)
        )

    def test_terminal_cannot_transition(self) -> None:
        self.assertFalse(
            can_transition(BudgetReservationStatus.SETTLED, BudgetReservationStatus.CONSUMING)
        )
        self.assertFalse(
            can_transition(BudgetReservationStatus.RELEASED, BudgetReservationStatus.SETTLED)
        )


class ResolveSettlementTests(unittest.TestCase):
    def test_exact_settles_by_actual(self) -> None:
        decision = resolve_settlement(
            ExactModelUsageV1(kind="EXACT", input_tokens=80, output_tokens=40, source="PROVIDER")
        )
        self.assertEqual(decision.entry_kind, BudgetLedgerEntryKind.SETTLE)
        self.assertEqual(decision.status, BudgetReservationStatus.SETTLED)

    def test_estimated_settles_by_worst_case(self) -> None:
        decision = resolve_settlement(
            EstimatedModelUsageV1(
                kind="ESTIMATED_DEV",
                estimated_input_tokens=100,
                estimated_output_tokens=50,
                source="WORST_CASE_RESERVATION",
            )
        )
        self.assertEqual(decision.entry_kind, BudgetLedgerEntryKind.WORST_CASE_SETTLE)
        self.assertEqual(decision.status, BudgetReservationStatus.SETTLED)

    def test_unknown_keeps_reconciling(self) -> None:
        decision = resolve_settlement(UnknownModelUsageV1(kind="UNKNOWN"))
        self.assertIsNone(decision.entry_kind)
        self.assertEqual(decision.status, BudgetReservationStatus.RECONCILING)


class BudgetInsertGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_insert_rejects_non_reserved_status(self) -> None:
        repository = BudgetLedgerRepository(MagicMock())  # type: ignore[arg-type]
        reservation = make_reservation(status=BudgetReservationStatus.CONSUMING)
        entry = make_entry(reservation_id=reservation.reservation_id)
        with self.assertRaises(BudgetReservationStateViolation) as raised:
            await repository.insert_reserved(reservation, entry)
        self.assertEqual(raised.exception.code, "BUDGET_RESERVATION_INSERT_NOT_RESERVED")

    async def test_insert_rejects_entry_kind_mismatch(self) -> None:
        repository = BudgetLedgerRepository(MagicMock())  # type: ignore[arg-type]
        reservation = make_reservation()
        entry = make_entry(
            reservation_id=reservation.reservation_id, entry_kind=BudgetLedgerEntryKind.SETTLE
        )
        with self.assertRaises(BudgetLedgerContractError) as raised:
            await repository.insert_reserved(reservation, entry)
        self.assertEqual(str(raised.exception), "BUDGET_RESERVE_ENTRY_KIND_MISMATCH")

    async def test_insert_rejects_entry_delta_mismatch(self) -> None:
        repository = BudgetLedgerRepository(MagicMock())  # type: ignore[arg-type]
        reservation = make_reservation()
        entry = make_entry(
            reservation_id=reservation.reservation_id,
            delta=BudgetQuantityDeltaV1(input_tokens=99),
        )
        with self.assertRaises(BudgetLedgerContractError) as raised:
            await repository.insert_reserved(reservation, entry)
        self.assertEqual(str(raised.exception), "BUDGET_RESERVE_ENTRY_DELTA_MISMATCH")


class BudgetMigrationGuardTests(unittest.TestCase):
    def test_downgrade_guard_precedes_drop(self) -> None:
        spec = importlib.util.spec_from_file_location("budget_migration_guard", MIGRATION)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with (
            patch.object(module.op, "execute") as execute,
            patch.object(module.op, "drop_index", side_effect=RuntimeError("stop after guard")),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after guard"):
                module.downgrade()
        assert "BUDGET_LEDGER_DOWNGRADE_REQUIRES_BACKUP_RESTORE" in str(execute.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
