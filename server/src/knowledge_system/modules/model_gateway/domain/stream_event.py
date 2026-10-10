"""流式事件单一Pydantic源：项目稳定`ModelEvent`与Bifrost数据面原事件。

依据模型网关模块设计§4.3/§7：Bifrost只转发Provider SSE，稳定序号、错误归一、Usage和
ModelCall终态由项目Policy Adapter（`ProviderEventNormalizer`）生成并提交；Bifrost中断或
统计缺失不能直接修改业务终态。原事件只保存Provider转发的有界事实，不保存输入正文、Secret或
原包。终态Usage优先Provider实测（EXACT），缺失时用预留最坏值估算（ESTIMATED_DEV），两者皆无
记UNKNOWN。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .provider_error import ProviderErrorSource, ProviderErrorV1
from .usage import ExactModelUsageV1

Sequence = Annotated[int, Field(ge=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class ModelStartedEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: Literal["STARTED"]
    sequence: Sequence


class ModelDeltaEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: Literal["DELTA"]
    sequence: Sequence
    text_delta: str
    finish_reason: str | None = None


class ModelUsageEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: Literal["USAGE"]
    sequence: Sequence
    usage: ExactModelUsageV1


class ModelCompletedEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: Literal["COMPLETED"]
    sequence: Sequence
    finish_reason: str | None = None


class ModelErrorEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: Literal["ERROR"]
    sequence: Sequence
    error: ProviderErrorV1


ModelEventV1 = Annotated[
    ModelStartedEventV1
    | ModelDeltaEventV1
    | ModelUsageEventV1
    | ModelCompletedEventV1
    | ModelErrorEventV1,
    Field(discriminator="type"),
]
MODEL_EVENT_ADAPTER: TypeAdapter[ModelEventV1] = TypeAdapter(ModelEventV1)


class BifrostDeltaEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: Literal["DELTA"]
    chunk_id: str | None = None
    text_delta: str | None = None
    finish_reason: str | None = None


class BifrostUsageEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: Literal["USAGE"]
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt


class BifrostErrorEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: Literal["ERROR"]
    http_status: int | None = None
    source: ProviderErrorSource = ProviderErrorSource.HTTP_STATUS


BifrostEventV1 = Annotated[
    BifrostDeltaEventV1 | BifrostUsageEventV1 | BifrostErrorEventV1,
    Field(discriminator="type"),
]
BIFROST_EVENT_ADAPTER: TypeAdapter[BifrostEventV1] = TypeAdapter(BifrostEventV1)


__all__ = [
    "BIFROST_EVENT_ADAPTER",
    "MODEL_EVENT_ADAPTER",
    "BifrostDeltaEventV1",
    "BifrostErrorEventV1",
    "BifrostEventV1",
    "BifrostUsageEventV1",
    "ModelCompletedEventV1",
    "ModelDeltaEventV1",
    "ModelErrorEventV1",
    "ModelEventV1",
    "ModelStartedEventV1",
    "ModelUsageEventV1",
]
