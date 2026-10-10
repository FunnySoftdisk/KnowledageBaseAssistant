"""预算预留契约：`BudgetReservationV1`读模型、状态机与结算判定。

依据数据库设计§9.12的`workflow.budget_reservation`：预留状态为
`RESERVED/CONSUMING/SETTLED/RELEASED/EXPIRED/RECONCILING`，同一逻辑外调唯一；已知Usage按实际
结算、未知Usage/Effect按最坏预留结算或保持RECONCILING，暂停/取消/异常不重置累计计数。状态机
只允许正向推进：RESERVED→CONSUMING→SETTLED/RECONCILING，RESERVED→RELEASED/EXPIRED，
RECONCILING→SETTLED；终态不可再转移。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .budget_ledger import BudgetLedgerEntryKind
from .budget_quantity import BudgetQuantityV1
from .usage import ModelUsageV1, SettlementKind, resolve_settlement_kind

Sha256 = Annotated[str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]


class BudgetReservationStatus(StrEnum):
    RESERVED = "RESERVED"
    CONSUMING = "CONSUMING"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"
    RECONCILING = "RECONCILING"


_TRANSITIONS: dict[BudgetReservationStatus, frozenset[BudgetReservationStatus]] = {
    BudgetReservationStatus.RESERVED: frozenset(
        {
            BudgetReservationStatus.CONSUMING,
            BudgetReservationStatus.RELEASED,
            BudgetReservationStatus.EXPIRED,
        }
    ),
    BudgetReservationStatus.CONSUMING: frozenset(
        {
            BudgetReservationStatus.SETTLED,
            BudgetReservationStatus.RECONCILING,
        }
    ),
    BudgetReservationStatus.RECONCILING: frozenset({BudgetReservationStatus.SETTLED}),
    BudgetReservationStatus.SETTLED: frozenset(),
    BudgetReservationStatus.RELEASED: frozenset(),
    BudgetReservationStatus.EXPIRED: frozenset(),
}

_TERMINAL = frozenset(
    {
        BudgetReservationStatus.SETTLED,
        BudgetReservationStatus.RELEASED,
        BudgetReservationStatus.EXPIRED,
    }
)


class BudgetReservationStateViolation(RuntimeError):
    """非法预留状态转移；`code`稳定供上层对账。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def can_transition(
    from_status: BudgetReservationStatus, to_status: BudgetReservationStatus
) -> bool:
    return to_status in _TRANSITIONS[from_status]


def is_terminal(status: BudgetReservationStatus) -> bool:
    return status in _TERMINAL


def require_transition(
    from_status: BudgetReservationStatus, to_status: BudgetReservationStatus
) -> None:
    if not can_transition(from_status, to_status):
        raise BudgetReservationStateViolation("BUDGET_RESERVATION_STATE_TRANSITION_INVALID")


class BudgetReservationV1(BaseModel):
    """预留读模型；字段与`workflow.budget_reservation`列一一对应。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    reservation_id: UUID
    task_id: UUID
    branch_id: UUID | None = None
    delegation_id: UUID | None = None
    task_attempt_id: UUID | None = None
    model_call_id: UUID | None = None
    action_attempt_id: UUID | None = None
    invocation_id: UUID | None = None
    account_id: UUID
    reservation_version: int = Field(ge=1)
    reserved: BudgetQuantityV1
    settled: BudgetQuantityV1
    reservation_digest: Sha256
    status: BudgetReservationStatus
    expires_at: datetime | None = None
    created_at: datetime
    settled_at: datetime | None = None
    row_version: int = Field(ge=1)


class BudgetSettlementDecision(BaseModel):
    """结算判定：按Usage结算口径归纳分录种类与预留终态；RECONCILING不产生结算分录。"""

    model_config = ConfigDict(frozen=True, strict=True)

    entry_kind: BudgetLedgerEntryKind | None
    status: BudgetReservationStatus


def resolve_settlement(usage: ModelUsageV1) -> BudgetSettlementDecision:
    """EXACT→SETTLE+SETTLED、ESTIMATED_DEV→WORST_CASE_SETTLE+SETTLED、UNKNOWN→RECONCILING。"""
    kind = resolve_settlement_kind(usage)
    if kind is SettlementKind.RECONCILING:
        return BudgetSettlementDecision(
            entry_kind=None, status=BudgetReservationStatus.RECONCILING
        )
    entry_kind = (
        BudgetLedgerEntryKind.SETTLE
        if kind is SettlementKind.KNOWN
        else BudgetLedgerEntryKind.WORST_CASE_SETTLE
    )
    return BudgetSettlementDecision(
        entry_kind=entry_kind, status=BudgetReservationStatus.SETTLED
    )


__all__ = [
    "BudgetReservationStateViolation",
    "BudgetReservationStatus",
    "BudgetReservationV1",
    "BudgetSettlementDecision",
    "can_transition",
    "is_terminal",
    "require_transition",
    "resolve_settlement",
]
