"""ModelCall业务账本契约的单一Pydantic源。

依据模型网关模块设计§12与数据库设计§10.4：`workflow.model_call`是模型调用业务账本唯一权威，
保存稳定`model_call_id`、批准/实际端点与模型、Context Pack引用、Token Accounting与Usage、
预算预留引用、耗时、状态与稳定错误码。Bifrost只提供Request ID/Token/Cost/瞬时状态证据，
不得反向覆盖项目业务事实；网络结果未知保持`UNKNOWN`，经Provider Request ID或幂等记录对账。
本类型是持久化读模型，字段与`workflow.model_call`列一一对应；不保存API Key、思维链或默认完整
Prompt/Response。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .call_state import ModelCallState
from .token_accounting import TokenAccountingV1
from .usage import ModelUsageV1

Sha256 = Annotated[str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(ge=0)]


class DataScope(StrEnum):
    PUBLIC_DEVELOPMENT = "PUBLIC_DEVELOPMENT"
    APPROVED_EXTERNAL_TRIAL = "APPROVED_EXTERNAL_TRIAL"
    INTERNAL_PRODUCTION = "INTERNAL_PRODUCTION"
    PUBLIC_EVALUATION = "PUBLIC_EVALUATION"


class ModelCallV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_call_id: UUID
    task_id: UUID | None = None
    attempt_id: UUID | None = None
    action_attempt_id: UUID | None = None
    branch_id: UUID | None = None
    delegation_id: UUID | None = None
    execution_generation: int | None = None
    cancellation_generation: int | None = None
    approved_endpoint_id: UUID | None = None
    actual_endpoint_id: UUID | None = None
    capability_alias: str = Field(min_length=1)
    approved_model_id: str = Field(min_length=1)
    approved_model_revision: str = Field(min_length=1)
    actual_provider: str | None = None
    actual_model_id: str | None = None
    actual_model_revision: str | None = None
    bifrost_request_id: str | None = None
    provider_request_id: str | None = None
    prompt_version: str = Field(min_length=1)
    tool_schema_versions: dict[str, str] = Field(default_factory=dict)
    context_pack_id: UUID
    context_policy_version: str = Field(min_length=1)
    authorization_version: int
    grant_id: UUID | None = None
    runtime_version: str = Field(min_length=1)
    plan_version: int | None = None
    agent_manifest_version: str = Field(min_length=1)
    conversation_memory_version: NonNegativeInt
    user_private_memory_version: NonNegativeInt
    task_memory_version: NonNegativeInt
    token_accounting: TokenAccountingV1
    output_schema_id: str | None = None
    output_schema_version: str | None = None
    data_scope: DataScope
    request_hash: Sha256
    response_hash: Sha256 | None = None
    usage: ModelUsageV1
    cost_amount: Decimal | None = None
    cost_currency: str | None = None
    budget_reservation_id: UUID | None = None
    rate_card_version: str | None = None
    exchange_rate_snapshot_version: str | None = None
    queue_ms: NonNegativeInt | None = None
    ttft_ms: NonNegativeInt | None = None
    duration_ms: NonNegativeInt | None = None
    status: ModelCallState
    error_code: str | None = None
    created_at: datetime


__all__ = ["DataScope", "ModelCallV1"]
