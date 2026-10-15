"""最后跳出网许可的签发端口。

`ModelGatewayService`（Policy Adapter）在把流交给Bifrost前，调用此端口把一次性许可持久化为
`READY`，供Bridge在最后跳`admit()`读取并消耗。签发请求只携带Policy Adapter已批准的事实——
不透明调用标识、批准路由引用、物理模型与冻结Body摘要；`secret_ref`/`secret_version`由签发方
在基础设施层解析（身份HMAC派生路径 + OpenBao元数据版本），绝不回传Policy Adapter、绝不进入
本端口载荷。纯端口契约，不接网络/ORM/密钥。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field

from knowledge_system.modules.tasking.domain.core_types import StrictContractV1

from ..domain.egress_admission import EgressBodyDigestV1


class EgressPermitIssuanceDisposition(StrEnum):
    INSERTED = "INSERTED"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"


class EgressPermitIssuanceRequestV1(StrictContractV1):
    """许可签发请求；不含Secret正文、SecretRef或Provider Key。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_call_id: UUID
    route_ref: str = Field(min_length=1, max_length=256)
    provider: Literal["alibaba_bailian"]
    provider_model: str = Field(min_length=1, max_length=192)
    body: EgressBodyDigestV1
    authorization_version: int = Field(ge=1)
    execution_generation: int = Field(ge=1)
    cancellation_generation: int = Field(ge=0)
    expires_at: datetime


class EgressPermitIssuanceResultV1(StrictContractV1):
    """许可签发结果；只证明许可已持久化，不回传许可内SecretRef/SecretVersion。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    disposition: EgressPermitIssuanceDisposition


class EgressPermitIssuer(Protocol):
    """一次性出网许可签发端口；实现位于基础设施层，把许可落为`READY`。"""

    async def issue_ready(
        self, request: EgressPermitIssuanceRequestV1
    ) -> EgressPermitIssuanceResultV1: ...


__all__ = [
    "EgressPermitIssuanceDisposition",
    "EgressPermitIssuanceRequestV1",
    "EgressPermitIssuanceResultV1",
    "EgressPermitIssuer",
]
