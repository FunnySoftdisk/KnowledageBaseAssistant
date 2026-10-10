"""`workflow.budget_reservation`/`workflow.budget_ledger_entry`账本SQLAlchemy适配器。

实现`BudgetLedger`端口：预留按主键幂等（`INSERT ... ON CONFLICT (id) DO NOTHING`，重复
`reservation_id`返回既有行、不重复计入`RESERVE`分录），状态转移按`from_status +
expected_row_version`CAS并落地`settled_at`，冲突精确区分不存在/非预期状态/行版本陈旧；账本分录
只追加、按`entry_id`幂等。五维嵌套量值对象在此与扁平预留/增量列互转。本模块不提交事务，由上层
UoW/服务在合适边界提交。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Select, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_system.modules.model_gateway.application.budget import (
    BudgetReservationInsertDisposition,
    BudgetReservationInsertResult,
    BudgetReservationTransitionConflict,
    BudgetReservationTransitionResult,
)
from knowledge_system.modules.model_gateway.domain.budget_ledger import (
    BudgetLedgerEntryKind,
    BudgetLedgerEntryV1,
)
from knowledge_system.modules.model_gateway.domain.budget_quantity import (
    BudgetQuantityDeltaV1,
    BudgetQuantityV1,
)
from knowledge_system.modules.model_gateway.domain.budget_reservation import (
    BudgetReservationStateViolation,
    BudgetReservationStatus,
    BudgetReservationV1,
    is_terminal,
    require_transition,
)

from .budget_models import BudgetLedgerEntryRecord, BudgetReservationRecord


class BudgetRecordIntegrityError(RuntimeError):
    """已持久化列违反预留/分录不变式，无法还原为读模型。"""


class BudgetLedgerContractError(RuntimeError):
    """端口调用违反账本契约（如RESERVE分录与预留不一致）。"""


def _require_int(value: int | None, column: str) -> int:
    if value is None:
        raise BudgetRecordIntegrityError(f"BUDGET_NULL_{column}")
    return value


def _require_decimal(value: Decimal | None, column: str) -> Decimal:
    if value is None:
        raise BudgetRecordIntegrityError(f"BUDGET_NULL_{column}")
    return value


def _flatten_quantity(quantity: BudgetQuantityV1, prefix: str) -> dict[str, object]:
    return {
        f"{prefix}_model_call_count": quantity.model_call_count,
        f"{prefix}_input_tokens": quantity.input_tokens,
        f"{prefix}_output_tokens": quantity.output_tokens,
        f"{prefix}_tool_count": quantity.tool_count,
        f"{prefix}_cost_amount": quantity.cost_amount,
    }


def _quantity_from_record(record: BudgetReservationRecord, prefix: str) -> BudgetQuantityV1:
    return BudgetQuantityV1(
        model_call_count=_require_int(
            getattr(record, f"{prefix}_model_call_count"), f"{prefix.upper()}_MODEL_CALL_COUNT"
        ),
        input_tokens=_require_int(
            getattr(record, f"{prefix}_input_tokens"), f"{prefix.upper()}_INPUT_TOKENS"
        ),
        output_tokens=_require_int(
            getattr(record, f"{prefix}_output_tokens"), f"{prefix.upper()}_OUTPUT_TOKENS"
        ),
        tool_count=_require_int(
            getattr(record, f"{prefix}_tool_count"), f"{prefix.upper()}_TOOL_COUNT"
        ),
        cost_amount=_require_decimal(
            getattr(record, f"{prefix}_cost_amount"), f"{prefix.upper()}_COST_AMOUNT"
        ),
    )


def _flatten_delta(delta: BudgetQuantityDeltaV1) -> dict[str, object]:
    return {
        "model_call_count_delta": delta.model_call_count,
        "input_tokens_delta": delta.input_tokens,
        "output_tokens_delta": delta.output_tokens,
        "tool_count_delta": delta.tool_count,
        "cost_amount_delta": delta.cost_amount,
    }


def _delta_from_record(record: BudgetLedgerEntryRecord) -> BudgetQuantityDeltaV1:
    return BudgetQuantityDeltaV1(
        model_call_count=_require_int(record.model_call_count_delta, "MODEL_CALL_COUNT_DELTA"),
        input_tokens=_require_int(record.input_tokens_delta, "INPUT_TOKENS_DELTA"),
        output_tokens=_require_int(record.output_tokens_delta, "OUTPUT_TOKENS_DELTA"),
        tool_count=_require_int(record.tool_count_delta, "TOOL_COUNT_DELTA"),
        cost_amount=_require_decimal(record.cost_amount_delta, "COST_AMOUNT_DELTA"),
    )


def reservation_record_from_v1(reservation: BudgetReservationV1) -> BudgetReservationRecord:
    reserved = _flatten_quantity(reservation.reserved, "reserved")
    settled = _flatten_quantity(reservation.settled, "settled")
    return BudgetReservationRecord(
        id=reservation.reservation_id,
        task_id=reservation.task_id,
        branch_id=reservation.branch_id,
        delegation_id=reservation.delegation_id,
        task_attempt_id=reservation.task_attempt_id,
        model_call_id=reservation.model_call_id,
        action_attempt_id=reservation.action_attempt_id,
        invocation_id=reservation.invocation_id,
        account_id=reservation.account_id,
        reservation_version=reservation.reservation_version,
        reservation_digest=reservation.reservation_digest,
        status=reservation.status.value,
        expires_at=reservation.expires_at,
        created_at=reservation.created_at,
        settled_at=reservation.settled_at,
        row_version=reservation.row_version,
        **reserved,
        **settled,
    )


def reservation_v1_from_record(record: BudgetReservationRecord) -> BudgetReservationV1:
    return BudgetReservationV1(
        reservation_id=record.id,
        task_id=record.task_id,
        branch_id=record.branch_id,
        delegation_id=record.delegation_id,
        task_attempt_id=record.task_attempt_id,
        model_call_id=record.model_call_id,
        action_attempt_id=record.action_attempt_id,
        invocation_id=record.invocation_id,
        account_id=record.account_id,
        reservation_version=record.reservation_version,
        reserved=_quantity_from_record(record, "reserved"),
        settled=_quantity_from_record(record, "settled"),
        reservation_digest=record.reservation_digest,
        status=BudgetReservationStatus(record.status),
        expires_at=record.expires_at,
        created_at=record.created_at,
        settled_at=record.settled_at,
        row_version=record.row_version,
    )


def entry_record_from_v1(entry: BudgetLedgerEntryV1) -> BudgetLedgerEntryRecord:
    return BudgetLedgerEntryRecord(
        entry_id=entry.entry_id,
        task_id=entry.task_id,
        account_id=entry.account_id,
        reservation_id=entry.reservation_id,
        entry_kind=entry.entry_kind.value,
        currency=entry.currency,
        rate_card_version=entry.rate_card_version,
        exchange_rate_snapshot_version=entry.exchange_rate_snapshot_version,
        caused_by_event_id=entry.caused_by_event_id,
        created_at=entry.created_at,
        **_flatten_delta(entry.delta),
    )


def entry_v1_from_record(record: BudgetLedgerEntryRecord) -> BudgetLedgerEntryV1:
    return BudgetLedgerEntryV1(
        entry_id=record.entry_id,
        task_id=record.task_id,
        account_id=record.account_id,
        reservation_id=record.reservation_id,
        entry_kind=BudgetLedgerEntryKind(record.entry_kind),
        delta=_delta_from_record(record),
        currency=record.currency,
        rate_card_version=record.rate_card_version,
        exchange_rate_snapshot_version=record.exchange_rate_snapshot_version,
        caused_by_event_id=record.caused_by_event_id,
        created_at=record.created_at,
    )


class BudgetLedgerRepository:
    """预算预留与账本适配器；持有同Session，不提交、不开启事务。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_reserved(
        self, reservation: BudgetReservationV1, reserve_entry: BudgetLedgerEntryV1
    ) -> BudgetReservationInsertResult:
        if reservation.status != BudgetReservationStatus.RESERVED:
            raise BudgetReservationStateViolation("BUDGET_RESERVATION_INSERT_NOT_RESERVED")
        _require_reserve_entry(reservation, reserve_entry)

        record = reservation_record_from_v1(reservation)
        values = {
            column.name: getattr(record, column.name)
            for column in BudgetReservationRecord.__table__.columns
        }
        statement = (
            pg_insert(BudgetReservationRecord)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[BudgetReservationRecord.id])
            .returning(BudgetReservationRecord.id)
        )
        inserted_id = (await self._session.execute(statement)).scalar_one_or_none()
        if inserted_id is None:
            existing = await self._get_record_by_id(reservation.reservation_id)
            if existing is None:
                raise BudgetRecordIntegrityError("BUDGET_RESERVATION_INSERT_CONFLICT_MISSING_ROW")
            return BudgetReservationInsertResult(
                disposition=BudgetReservationInsertDisposition.ALREADY_PRESENT,
                reservation=reservation_v1_from_record(existing),
                row_version=existing.row_version,
            )
        await self.append_entry(reserve_entry)
        return BudgetReservationInsertResult(
            disposition=BudgetReservationInsertDisposition.INSERTED,
            reservation=reservation,
            row_version=1,
        )

    async def transition_status(
        self,
        reservation_id: UUID,
        *,
        from_status: BudgetReservationStatus,
        to_status: BudgetReservationStatus,
        expected_row_version: int,
        settled: BudgetQuantityV1 | None = None,
    ) -> BudgetReservationTransitionResult:
        require_transition(from_status, to_status)
        if to_status == BudgetReservationStatus.SETTLED and settled is None:
            raise BudgetLedgerContractError("BUDGET_SETTLE_REQUIRES_SETTLED_QUANTITY")
        values = _transition_values(settled, to_status)
        statement = (
            update(BudgetReservationRecord)
            .where(
                BudgetReservationRecord.id == reservation_id,
                BudgetReservationRecord.status == from_status.value,
                BudgetReservationRecord.row_version == expected_row_version,
            )
            .values(**values)
            .returning(BudgetReservationRecord.id)
        )
        committed_id = (await self._session.execute(statement)).scalar_one_or_none()
        if committed_id is not None:
            record = await self._get_record_by_id(reservation_id)
            assert record is not None
            return BudgetReservationTransitionResult(
                committed=True, reservation=reservation_v1_from_record(record)
            )

        record = await self._get_record_by_id(reservation_id)
        if record is None:
            reason = BudgetReservationTransitionConflict.NOT_FOUND
        elif BudgetReservationStatus(record.status) != from_status:
            reason = BudgetReservationTransitionConflict.NOT_IN_STATE
        else:
            reason = BudgetReservationTransitionConflict.ROW_VERSION_CONFLICT
        return BudgetReservationTransitionResult(committed=False, conflict_reason=reason)

    async def append_entry(self, entry: BudgetLedgerEntryV1) -> BudgetLedgerEntryV1:
        record = entry_record_from_v1(entry)
        values = {
            column.name: getattr(record, column.name)
            for column in BudgetLedgerEntryRecord.__table__.columns
        }
        statement = (
            pg_insert(BudgetLedgerEntryRecord)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[BudgetLedgerEntryRecord.entry_id])
            .returning(BudgetLedgerEntryRecord.entry_id)
        )
        inserted_id = (await self._session.execute(statement)).scalar_one_or_none()
        if inserted_id is not None:
            return entry
        existing = await self._get_entry_by_id(entry.entry_id)
        if existing is None:
            raise BudgetRecordIntegrityError("BUDGET_ENTRY_INSERT_CONFLICT_MISSING_ROW")
        return entry_v1_from_record(existing)

    async def get_by_id(self, reservation_id: UUID) -> BudgetReservationV1 | None:
        record = await self._get_record_by_id(reservation_id)
        return None if record is None else reservation_v1_from_record(record)

    async def get_by_model_call_id(self, model_call_id: UUID) -> BudgetReservationV1 | None:
        statement: Select[tuple[BudgetReservationRecord]] = select(
            BudgetReservationRecord
        ).where(BudgetReservationRecord.model_call_id == model_call_id)
        record = (await self._session.scalars(statement)).first()
        return None if record is None else reservation_v1_from_record(record)

    async def list_entries(self, reservation_id: UUID) -> list[BudgetLedgerEntryV1]:
        statement: Select[tuple[BudgetLedgerEntryRecord]] = select(
            BudgetLedgerEntryRecord
        ).where(BudgetLedgerEntryRecord.reservation_id == reservation_id).order_by(
            BudgetLedgerEntryRecord.created_at, BudgetLedgerEntryRecord.entry_id
        )
        records = (await self._session.scalars(statement)).all()
        return [entry_v1_from_record(record) for record in records]

    async def _get_record_by_id(self, reservation_id: UUID) -> BudgetReservationRecord | None:
        statement: Select[tuple[BudgetReservationRecord]] = select(
            BudgetReservationRecord
        ).where(BudgetReservationRecord.id == reservation_id)
        return (await self._session.scalars(statement)).first()

    async def _get_entry_by_id(self, entry_id: UUID) -> BudgetLedgerEntryRecord | None:
        statement: Select[tuple[BudgetLedgerEntryRecord]] = select(
            BudgetLedgerEntryRecord
        ).where(BudgetLedgerEntryRecord.entry_id == entry_id)
        return (await self._session.scalars(statement)).first()


def _require_reserve_entry(
    reservation: BudgetReservationV1, entry: BudgetLedgerEntryV1
) -> None:
    if entry.entry_kind is not BudgetLedgerEntryKind.RESERVE:
        raise BudgetLedgerContractError("BUDGET_RESERVE_ENTRY_KIND_MISMATCH")
    if entry.reservation_id != reservation.reservation_id:
        raise BudgetLedgerContractError("BUDGET_RESERVE_ENTRY_RESERVATION_MISMATCH")
    if entry.delta != reservation.reserved.as_delta():
        raise BudgetLedgerContractError("BUDGET_RESERVE_ENTRY_DELTA_MISMATCH")


def _transition_values(
    settled: BudgetQuantityV1 | None, to_status: BudgetReservationStatus
) -> dict[str, object]:
    values: dict[str, object] = {
        "status": to_status.value,
        "row_version": BudgetReservationRecord.row_version + 1,
    }
    if to_status == BudgetReservationStatus.SETTLED:
        values["settled_at"] = datetime.now(UTC)
    if is_terminal(to_status) and settled is not None:
        values.update(_flatten_quantity(settled, "settled"))
    return values


__all__ = [
    "BudgetLedgerContractError",
    "BudgetLedgerRepository",
    "BudgetRecordIntegrityError",
    "entry_record_from_v1",
    "entry_v1_from_record",
    "reservation_record_from_v1",
    "reservation_v1_from_record",
]
