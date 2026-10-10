"""最后跳出网准入端口（DEV-04F离线切片）。

依据模型网关设计§1.2.1与ADR-0002§5：ModelEgressBridge在构造最终Provider鉴权前，调用项目
ModelGatewayService内部出网准入方法——控制服务读PG当前ModelCall/许可/Route/SecretRef/Generation/
期限并CAS登记发送资格，核对实际Body与冻结投影；不递归调用LLM、不持PG事务锁等待HTTP。本端口只
冻结签名与请求/决策载荷；Secret只在Broker/Bridge内存及最后跳Provider Authorization使用，绝不
出现在本端口载荷中。持久化CAS与真实Bridge实现属基础设施，留待真实交接。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ..domain.egress_admission import EgressBodyDigestV1


class EgressAdmissionRequestV1(BaseModel):
    """Bridge发起最后跳出网准入的请求；只含不透明调用标识、批准路由引用与实际Body摘要。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_call_id: UUID
    route_ref: str = Field(min_length=1)
    body: EgressBodyDigestV1


class EgressAdmissionOutcome(StrEnum):
    ADMITTED = "ADMITTED"
    REJECTED = "REJECTED"
    RECONCILING = "RECONCILING"


class EgressAdmissionDecisionV1(BaseModel):
    """出网准入决策；`REJECTED`/`RECONCILING`时`reason_code`为稳定可公开拒绝码。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    outcome: EgressAdmissionOutcome
    reason_code: str | None = None


class ModelEgressAdmission(Protocol):
    """最后跳出网准入端口；实现位于基础设施层，Bridge在最后跳注入前调用。"""

    async def admit(
        self, *, request: EgressAdmissionRequestV1
    ) -> EgressAdmissionDecisionV1: ...


__all__ = [
    "EgressAdmissionDecisionV1",
    "EgressAdmissionOutcome",
    "EgressAdmissionRequestV1",
    "ModelEgressAdmission",
]
