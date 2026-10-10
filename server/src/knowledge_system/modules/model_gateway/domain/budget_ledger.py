"""预算账本分录契约：`BudgetLedgerEntryV1`与`BudgetLedgerEntryKind`，以及结算分账与守恒校验。

依据数据库设计§9.12的`workflow.budget_ledger_entry`：账本只追加，`entry_kind`限定
`ALLOCATE/RESERVE/SETTLE/RELEASE/RETURN/WORST_CASE_SETTLE`六种；每维总账守恒——一次预留的
`RESERVE`正增量必须被`SETTLE`/`WORST_CASE_SETTLE`（实际或最坏实耗）与`RELEASE`/`RETURN`
（未耗用退还）恰好对平，不能凭空多扣或少退。`settle_deltas`按构造保证守恒：已知实耗按实际
结算、退还差额，未知实耗按最坏预留结算不退还。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

from .budget_quantity import DIMENSIONS, BudgetQuantityDeltaV1, BudgetQuantityV1

Sha256 = Annotated[str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]


class BudgetLedgerEntryKind(StrEnum):
    ALLOCATE = "ALLOCATE"
    RESERVE = "RESERVE"
    SETTLE = "SETTLE"
    RELEASE = "RELEASE"
    RETURN = "RETURN"
    WORST_CASE_SETTLE = "WORST_CASE_SETTLE"


class BudgetLedgerEntryV1(BaseModel):
    """账本分录读模型；字段与`workflow.budget_ledger_entry`列一一对应。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    entry_id: UUID
    task_id: UUID
    account_id: UUID
    reservation_id: UUID | None = None
    entry_kind: BudgetLedgerEntryKind
    delta: BudgetQuantityDeltaV1
    currency: str = "CNY"
    rate_card_version: str | None = None
    exchange_rate_snapshot_version: str | None = None
    caused_by_event_id: UUID | None = None
    created_at: datetime


class BudgetSettlementOverrunError(ValueError):
    """实耗超过预留上限：不能直接SETTLE，必须先增配或保持RECONCILING。"""


def _overrun_dimensions(reserved: BudgetQuantityV1, settled: BudgetQuantityV1) -> list[str]:
    return [
        name
        for name in DIMENSIONS
        if getattr(settled, name) > getattr(reserved, name)
    ]


def settle_deltas(
    reserved: BudgetQuantityV1, settled: BudgetQuantityV1
) -> tuple[BudgetQuantityDeltaV1, BudgetQuantityDeltaV1]:
    """结算分账：(SETTLE正增量=实耗, RETURN负增量=-(预留-实耗))；每维预留=实耗+退还。

    实耗不得超过预留，否则抛`BudgetSettlementOverrunError`，由上层走RECONCILING或先增配。
    """
    overrun = _overrun_dimensions(reserved, settled)
    if overrun:
        raise BudgetSettlementOverrunError(
            f"BUDGET_SETTLEMENT_OVERRUN:{','.join(overrun)}"
        )
    settle = settled.as_delta()
    ret = reserved.subtract(settled).as_delta().negate()
    return settle, ret


def conservation_gap(
    reserved: BudgetQuantityV1,
    consumed: BudgetQuantityV1,
    returned: BudgetQuantityV1,
) -> BudgetQuantityDeltaV1:
    """每维残差=预留-实耗-退还；闭合分录的残差必须全零。"""
    gap = reserved.subtract(consumed).subtract(returned)
    return BudgetQuantityDeltaV1(
        model_call_count=gap.model_call_count,
        input_tokens=gap.input_tokens,
        output_tokens=gap.output_tokens,
        tool_count=gap.tool_count,
        cost_amount=gap.cost_amount,
    )


def is_conserved(
    reserved: BudgetQuantityV1,
    consumed: BudgetQuantityV1,
    returned: BudgetQuantityV1,
) -> bool:
    return all(
        getattr(conservation_gap(reserved, consumed, returned), name) == 0
        for name in DIMENSIONS
    )


__all__ = [
    "BudgetLedgerEntryKind",
    "BudgetLedgerEntryV1",
    "BudgetSettlementOverrunError",
    "conservation_gap",
    "is_conserved",
    "settle_deltas",
]
