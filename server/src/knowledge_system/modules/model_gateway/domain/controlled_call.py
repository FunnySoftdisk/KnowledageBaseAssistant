"""受控单次模型调用的输入/输出契约。

`ModelGatewayService`（Policy Adapter）消费`ControlledCallPlanV1`，产出`ControlledCallResultV1`。
计划只携带不透明标识、冻结版本、预算上限、非敏感Prompt与结构化输出Schema；不含Secret、端点
URL或Provider Key。结果只携带稳定终态、归一文本、结算Usage与可公开错误码。纯值对象，不接
网络/ORM/密钥。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import ConfigDict, Field

from knowledge_system.modules.tasking.domain.core_types import StrictContractV1

from .budget_quantity import BudgetQuantityV1
from .call_state import ModelCallState
from .chat_request import ChatJsonSchemaV1, ChatMessageV1
from .model_call import Sha256
from .token_accounting import TokenAccountingV1
from .usage import ModelUsageV1


class ControlledCallPlanV1(StrictContractV1):
    """一次受控调用的完整输入；调用方预生成`model_call_id`作为幂等令牌。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_call_id: UUID
    task_id: UUID
    account_id: UUID = Field(description="预算账户；五维预留按此账户结算")
    context_pack_id: UUID
    context_pack_hash: Sha256
    context_pack_input_digest: Sha256
    capability_alias: str = Field(min_length=1, max_length=64)
    messages: tuple[ChatMessageV1, ...] = Field(min_length=1, max_length=64)
    max_tokens: int = Field(gt=0, le=32768)
    response_schema: ChatJsonSchemaV1
    token_accounting: TokenAccountingV1
    reserved: BudgetQuantityV1
    prompt_version: str = Field(min_length=1)
    context_policy_version: str = Field(min_length=1)
    runtime_version: str = Field(min_length=1)
    agent_manifest_version: str = Field(min_length=1)
    authorization_version: int = Field(ge=1)
    execution_generation: int = Field(ge=1)
    cancellation_generation: int = Field(ge=0)
    expires_in_seconds: int = Field(gt=0)


class ControlledCallResultV1(StrictContractV1):
    """一次受控调用的结果；`text`为归一后完整文本（结构化输出时为JSON字符串）。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_call_id: UUID
    status: ModelCallState
    text: str
    usage: ModelUsageV1
    error_code: str | None = None


class ControlledCallFailure(RuntimeError):
    """编排前置校验/许可签发失败；`code`稳定可公开，异常不携带Prompt正文或Secret。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


__all__ = [
    "ControlledCallFailure",
    "ControlledCallPlanV1",
    "ControlledCallResultV1",
]
