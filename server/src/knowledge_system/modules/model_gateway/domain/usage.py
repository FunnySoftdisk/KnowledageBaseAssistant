"""ModelUsage三态账本分支的单一Pydantic源。

依据模型网关模块设计§11/§12：Token按`EXACT/ESTIMATED_DEV/UNKNOWN`闭合分支，Provider Usage
实测并对账，未知账单不记假零、不伪装Provider精确值。EXACT为Provider报告实测Token，ESTIMATED_DEV
按预留最坏值保守估算，UNKNOWN不携带任何Token/费用字段。结算口径由`resolve_settlement_kind`
归纳：EXACT按KNOWN、ESTIMATED_DEV按WORST_CASE、UNKNOWN按RECONCILING对账。
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

NonNegativeInt = Annotated[int, Field(ge=0)]


class ExactModelUsageV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["EXACT"]
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    source: Literal["PROVIDER"]


class EstimatedModelUsageV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["ESTIMATED_DEV"]
    estimated_input_tokens: NonNegativeInt
    estimated_output_tokens: NonNegativeInt
    source: Literal["WORST_CASE_RESERVATION"]


class UnknownModelUsageV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["UNKNOWN"]


ModelUsageV1 = Annotated[
    ExactModelUsageV1 | EstimatedModelUsageV1 | UnknownModelUsageV1,
    Field(discriminator="kind"),
]
MODEL_USAGE_ADAPTER: TypeAdapter[ModelUsageV1] = TypeAdapter(ModelUsageV1)


class SettlementKind(StrEnum):
    KNOWN = "KNOWN"
    WORST_CASE = "WORST_CASE"
    RECONCILING = "RECONCILING"


def resolve_settlement_kind(usage: ModelUsageV1) -> SettlementKind:
    """EXACT按实测结算、ESTIMATED_DEV按最坏预留结算、UNKNOWN留待对账。"""
    if usage.kind == "EXACT":
        return SettlementKind.KNOWN
    if usage.kind == "ESTIMATED_DEV":
        return SettlementKind.WORST_CASE
    return SettlementKind.RECONCILING


__all__ = [
    "MODEL_USAGE_ADAPTER",
    "EstimatedModelUsageV1",
    "ExactModelUsageV1",
    "ModelUsageV1",
    "SettlementKind",
    "UnknownModelUsageV1",
    "resolve_settlement_kind",
]
