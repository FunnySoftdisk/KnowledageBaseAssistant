"""预算预留与账本写入端口。

`ModelGatewayService`（Policy Adapter）依赖此端口把`MODEL_REQUEST_ACCEPTED`事务的预算硬预留与
Provider返回后的结算落到`workflow.budget_reservation`/`workflow.budget_ledger_entry`，而把ORM/
Session隔离在基础设施层。写入端只暴露稳定`reservation_id`/`entry_id`与`row_version`作为幂等与
并发令牌：预留按主键幂等（重复`reservation_id`返回既有行、不重复计入`RESERVE`分录），状态转移
按`from_status + expected_row_version`CAS，冲突精确区分不存在/非预期状态/行版本陈旧；账本分录
只追加、按`entry_id`幂等，结算分账由领域`settle_deltas`保证每维预留=实耗+退还。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ..domain.budget_ledger import BudgetLedgerEntryV1
from ..domain.budget_quantity import BudgetQuantityV1
from ..domain.budget_reservation import (
    BudgetReservationStatus,
    BudgetReservationV1,
)


class BudgetReservationInsertDisposition(StrEnum):
    INSERTED = "INSERTED"
    ALREADY_PRESENT = "ALREADY_PRESENT"


class BudgetReservationInsertResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    disposition: BudgetReservationInsertDisposition
    reservation: BudgetReservationV1
    row_version: int


class BudgetReservationTransitionConflict(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    NOT_IN_STATE = "NOT_IN_STATE"
    ROW_VERSION_CONFLICT = "ROW_VERSION_CONFLICT"


class BudgetReservationTransitionResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    committed: bool
    reservation: BudgetReservationV1 | None = None
    conflict_reason: BudgetReservationTransitionConflict | None = None


class BudgetReservationTransitionUpdates(BaseModel):
    """一次状态转移可附加的事实；`None`表示不改变。"""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    settled: BudgetQuantityV1 | None = None


class BudgetLedger(Protocol):
    """预算预留与账本端口；实现位于基础设施层，永不自行提交事务。"""

    async def insert_reserved(
        self, reservation: BudgetReservationV1, reserve_entry: BudgetLedgerEntryV1
    ) -> BudgetReservationInsertResult: ...

    async def transition_status(
        self,
        reservation_id: UUID,
        *,
        from_status: BudgetReservationStatus,
        to_status: BudgetReservationStatus,
        expected_row_version: int,
        settled: BudgetQuantityV1 | None = None,
    ) -> BudgetReservationTransitionResult: ...

    async def append_entry(self, entry: BudgetLedgerEntryV1) -> BudgetLedgerEntryV1: ...

    async def get_by_id(self, reservation_id: UUID) -> BudgetReservationV1 | None: ...

    async def get_by_model_call_id(self, model_call_id: UUID) -> BudgetReservationV1 | None: ...

    async def list_entries(self, reservation_id: UUID) -> list[BudgetLedgerEntryV1]: ...


__all__ = [
    "BudgetLedger",
    "BudgetReservationInsertDisposition",
    "BudgetReservationInsertResult",
    "BudgetReservationTransitionConflict",
    "BudgetReservationTransitionResult",
    "BudgetReservationTransitionUpdates",
]
