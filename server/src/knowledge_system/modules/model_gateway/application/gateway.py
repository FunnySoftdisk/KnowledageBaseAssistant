"""模型调用账本写入/对账端口。

`ModelGatewayService`（Policy Adapter）依赖此端口把消费事务提交、Provider调用、流式推进与
终态收敛落到`workflow.model_call`，而把ORM/Session隔离在基础设施层。写入端只暴露稳定
`model_call_id`与`row_version`作为幂等与并发令牌：插入按主键幂等（重复`model_call_id`返回
既有记录、不新建第二条计费行），状态转移按`from_status + expected_row_version`做CAS，冲突时
精确区分不存在/非预期状态/行版本陈旧，供上层对账或重放。
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ..domain.call_state import ModelCallState
from ..domain.model_call import ModelCallV1, Sha256
from ..domain.usage import ModelUsageV1


class ModelCallInsertDisposition(StrEnum):
    INSERTED = "INSERTED"
    ALREADY_PRESENT = "ALREADY_PRESENT"


class ModelCallInsertResult(BaseModel):
    """插入结果；`row_version`是后续状态转移所需的CAS令牌。"""

    model_config = ConfigDict(frozen=True, strict=True)

    disposition: ModelCallInsertDisposition
    call: ModelCallV1
    row_version: int


class ModelCallTransitionConflict(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    NOT_IN_STATE = "NOT_IN_STATE"
    ROW_VERSION_CONFLICT = "ROW_VERSION_CONFLICT"


class ModelCallTransitionResult(BaseModel):
    """状态转移结果；失败时`conflict_reason`精确定位，供上层对账/重放。"""

    model_config = ConfigDict(frozen=True, strict=True)

    committed: bool
    call: ModelCallV1 | None = None
    conflict_reason: ModelCallTransitionConflict | None = None


class ModelCallTransitionUpdates(BaseModel):
    """一次状态转移可附加的事实；`None`表示不改变。字段在Provider调用/流/结算各阶段逐次补全。"""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    actual_endpoint_id: UUID | None = None
    actual_provider: str | None = None
    actual_model_id: str | None = None
    actual_model_revision: str | None = None
    bifrost_request_id: str | None = None
    provider_request_id: str | None = None
    response_hash: Sha256 | None = None
    usage: ModelUsageV1 | None = None
    cost_amount: Decimal | None = None
    cost_currency: str | None = None
    error_code: str | None = None
    queue_ms: int | None = Field(default=None, ge=0)
    ttft_ms: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)


class ModelCallLedger(Protocol):
    """模型调用账本端口；实现位于基础设施层，永不自行提交与调用无关的事务。"""

    async def insert_accepted(self, call: ModelCallV1) -> ModelCallInsertResult: ...

    async def transition_status(
        self,
        model_call_id: UUID,
        *,
        from_status: ModelCallState,
        to_status: ModelCallState,
        expected_row_version: int,
        updates: ModelCallTransitionUpdates | None = None,
    ) -> ModelCallTransitionResult: ...

    async def get_by_id(self, model_call_id: UUID) -> ModelCallV1 | None: ...

    async def get_by_provider_request_id(self, provider_request_id: str) -> ModelCallV1 | None: ...


__all__ = [
    "ModelCallInsertDisposition",
    "ModelCallInsertResult",
    "ModelCallLedger",
    "ModelCallTransitionConflict",
    "ModelCallTransitionResult",
    "ModelCallTransitionUpdates",
]
