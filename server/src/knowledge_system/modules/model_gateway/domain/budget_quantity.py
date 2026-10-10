"""预算五维量值对象：`BudgetQuantityV1`（绝对量，非负）与`BudgetQuantityDeltaV1`（账本增量，可负）。

依据数据库设计§9.12的`workflow.budget_account`/`budget_reservation`/`budget_ledger_entry`：五维为
模型调用次数、Input Token、Output Token、Tool调用次数、费用(CNY)。绝对量只承载上限/预留/实耗，
逐维非负；账本增量承载`ALLOCATE/RESERVE/SETTLE/RELEASE/RETURN/WORST_CASE_SETTLE`的带符号变动
（预留取正、释放/退还取负）。纯值对象，不依赖Web/ORM/SDK。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

NonNegativeInt = Annotated[int, Field(ge=0)]

# 五维顺序即账本列顺序，`settle_deltas`/守恒校验按此逐维比对。
DIMENSIONS: tuple[str, str, str, str, str] = (
    "model_call_count",
    "input_tokens",
    "output_tokens",
    "tool_count",
    "cost_amount",
)


class BudgetQuantityV1(BaseModel):
    """绝对量：预留上限或已结算实耗，逐维非负。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_call_count: NonNegativeInt = 0
    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    tool_count: NonNegativeInt = 0
    cost_amount: Decimal = Field(default=Decimal("0"), ge=0)

    def as_delta(self) -> BudgetQuantityDeltaV1:
        """绝对量 → 正向增量。"""
        return BudgetQuantityDeltaV1(
            model_call_count=self.model_call_count,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            tool_count=self.tool_count,
            cost_amount=self.cost_amount,
        )

    def subtract(self, other: BudgetQuantityV1) -> BudgetQuantityV1:
        """逐维相减；结果若出现负值即超额，由调用方决定按RECONCILING处理。"""
        return BudgetQuantityV1(
            model_call_count=self.model_call_count - other.model_call_count,
            input_tokens=self.input_tokens - other.input_tokens,
            output_tokens=self.output_tokens - other.output_tokens,
            tool_count=self.tool_count - other.tool_count,
            cost_amount=self.cost_amount - other.cost_amount,
        )


class BudgetQuantityDeltaV1(BaseModel):
    """账本增量：带符号的逐维变动，`None`语义用整数零/`Decimal("0")`表示。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_count: int = 0
    cost_amount: Decimal = Decimal("0")

    def negate(self) -> BudgetQuantityDeltaV1:
        return BudgetQuantityDeltaV1(
            model_call_count=-self.model_call_count,
            input_tokens=-self.input_tokens,
            output_tokens=-self.output_tokens,
            tool_count=-self.tool_count,
            cost_amount=-self.cost_amount,
        )


__all__ = ["DIMENSIONS", "BudgetQuantityDeltaV1", "BudgetQuantityV1"]
