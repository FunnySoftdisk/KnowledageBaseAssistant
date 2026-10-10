"""不可变配置投影契约：单一受控来源、Digest核对启用、禁止调用方覆盖路由真相。

依据模型网关模块设计§12与Bifrost配置边界：批准且持久化的Binding/Profile生成不可变配置投影，
配置Digest核对后启用；禁止在Bifrost管理界面维护第二套业务路由真相，不允许普通调用方覆盖
`base_url`、模型名、凭据或回退。本模块是纯规则，不接真实模型、不保存明文凭据——端点只存Secret
引用与批准路由事实；冻结后不原地改，变更生成新投影。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Sha256 = Annotated[str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]


class ConfigProjectionViolation(ValueError):
    """稳定code可公开；异常中不保存凭据正文、Secret或Provider原包。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ConfigProjectionStatus(StrEnum):
    DISABLED = "DISABLED"
    ENABLED = "ENABLED"


class EndpointProjectionV1(BaseModel):
    """端点投影：端点地址、提供方、能力集合、Secret引用、数据边界与超时；不含明文密钥。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    endpoint_id: UUID
    base_url: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    capability_aliases: tuple[str, ...] = Field(default_factory=tuple)
    secret_ref: str = Field(min_length=1)
    data_boundary: str = Field(min_length=1)
    timeout_ms: int = Field(gt=0)


class RouteProjectionV1(BaseModel):
    """能力别名到有序端点列表的路由投影。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    capability_alias: str = Field(min_length=1)
    endpoint_ids: tuple[UUID, ...]


class ModelBindingProjectionV1(BaseModel):
    """批准模型绑定投影：能力别名→准确模型ID/revision→端点。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    capability_alias: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    endpoint_id: UUID


class ConfigProjectionV1(BaseModel):
    """不可变配置投影；冻结后不原地改，任何变更生成新`projection_id`与新摘要。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    projection_id: UUID
    projection_digest: Sha256
    source_kind: Literal["BINDING", "PROFILE"]
    source_ref: str = Field(min_length=1)
    endpoints: tuple[EndpointProjectionV1, ...] = Field(default_factory=tuple)
    routes: tuple[RouteProjectionV1, ...] = Field(default_factory=tuple)
    model_bindings: tuple[ModelBindingProjectionV1, ...] = Field(default_factory=tuple)
    rate_card_version: str = Field(min_length=1)
    exchange_rate_snapshot_version: str | None = None
    frozen_at: datetime


class CallerOverrideV1(BaseModel):
    """调用方对路由真相的覆盖尝试；任何非空字段均被禁止。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    base_url: str | None = None
    model_id: str | None = None
    model_revision: str | None = None
    credential: str | None = None
    fallback: bool = False


def resolve_config_enablement(
    *, source_approved: bool, digest_verified: bool
) -> ConfigProjectionStatus:
    """配置仅在来源（Binding/Profile）已批准且投影Digest核对通过后启用。"""
    if source_approved and digest_verified:
        return ConfigProjectionStatus.ENABLED
    return ConfigProjectionStatus.DISABLED


def assert_no_caller_override(override: CallerOverrideV1 | None) -> None:
    """普通调用方不得覆盖base_url、模型名、凭据或回退；任何覆盖以稳定code失败关闭。"""
    if override is None:
        return
    if (
        override.base_url is not None
        or override.model_id is not None
        or override.model_revision is not None
        or override.credential is not None
        or override.fallback is True
    ):
        raise ConfigProjectionViolation("CONFIG_OVERRIDE_FORBIDDEN")


__all__ = [
    "CallerOverrideV1",
    "ConfigProjectionStatus",
    "ConfigProjectionV1",
    "ConfigProjectionViolation",
    "EndpointProjectionV1",
    "ModelBindingProjectionV1",
    "RouteProjectionV1",
    "assert_no_caller_override",
    "resolve_config_enablement",
]
