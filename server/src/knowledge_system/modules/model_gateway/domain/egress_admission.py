"""最后跳出网准入的纯核对规则。

依据模型网关设计§1.2.1与ADR-0002§5：ModelEgressBridge在构造最终Provider鉴权前，须核对实际Body
的物理模型/参数/Schema/输入摘要与冻结投影一致，不一致即外呼前拒绝。本模块是纯规则，只做摘要级
核对，不保存输入正文、Secret或Provider原包；实际读PG当前ModelCall/许可/Route/SecretRef/期限并
CAS登记发送资格的持久化实现属基础设施，留待真实交接。
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Sha256 = Annotated[str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]


class EgressBodyDigestV1(BaseModel):
    """出网Body与冻结投影的核对摘要：物理模型/参数/Schema/输入，不含正文与Secret。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_id: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    parameters_digest: Sha256
    schema_digest: Sha256 | None = None
    input_digest: Sha256


def verify_egress_projection(
    *, approved: EgressBodyDigestV1, actual: EgressBodyDigestV1
) -> str | None:
    """核对实际Body与冻结投影；返回`None`表示一致，否则稳定`EGRESS_PROJECTION_MISMATCH:*`拒绝码。"""
    if actual.model_id != approved.model_id:
        return "EGRESS_PROJECTION_MISMATCH:MODEL_ID"
    if actual.model_revision != approved.model_revision:
        return "EGRESS_PROJECTION_MISMATCH:MODEL_REVISION"
    if actual.parameters_digest != approved.parameters_digest:
        return "EGRESS_PROJECTION_MISMATCH:PARAMETERS"
    if actual.schema_digest != approved.schema_digest:
        return "EGRESS_PROJECTION_MISMATCH:SCHEMA"
    if actual.input_digest != approved.input_digest:
        return "EGRESS_PROJECTION_MISMATCH:INPUT"
    return None


__all__ = [
    "EgressBodyDigestV1",
    "verify_egress_projection",
]
