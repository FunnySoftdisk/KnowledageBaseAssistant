"""受控开发的模型绑定注册表。

`operations.model_endpoint/model_deployment/capability_route/configuration_revision`在数据库设计
§14仅提纲、非§9.12冻结列级定义，故`capability_route`尚未落地ORM。受控单次付费调用期间，绑定从
本冻结注册表解析（内容与`deploy/m0/approved-development-bindings.json`一致），而不是从PG读；
生产绑定仍须以PG配置投影为唯一权威。本模块是纯值对象，不接网络/ORM/密钥。
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import ConfigDict, Field

from knowledge_system.modules.tasking.domain.core_types import StrictContractV1


class ModelBindingViolation(ValueError):
    """稳定code可公开；异常不携带模型名、端点或凭据。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DevelopmentModelBindingV1(StrictContractV1):
    """一次受控调用的冻结物理模型绑定；无fallback、无自动重试、无凭据。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    capability_alias: str = Field(min_length=1, max_length=64)
    provider: Literal["alibaba_bailian"]
    bifrost_provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    region: str = Field(min_length=1, max_length=64)
    model_revision: str = Field(min_length=1, max_length=192)
    thinking: bool
    temperature: float = Field(ge=0, le=2)
    top_p: float = Field(gt=0, le=1)
    fallback: Literal[False] = False

    @property
    def bifrost_model(self) -> str:
        """Bifrost数据面协议里的`<bifrost路由提供者>/<model_revision>`标识。

        `provider`是物理上游(`alibaba_bailian`，决定Secret路径、许可与账本)，`bifrost_provider`
        是Bifrost配置里的内部路由提供者名(`bailian-bridge`，决定请求转发到哪条内部路由)；二者不同，
        不可互换。
        """
        return f"{self.bifrost_provider}/{self.model_revision}"


DEVELOPMENT_BINDINGS: Final[dict[str, DevelopmentModelBindingV1]] = {
    "planning_strong": DevelopmentModelBindingV1(
        capability_alias="planning_strong",
        provider="alibaba_bailian",
        bifrost_provider="bailian-bridge",
        region="cn-beijing",
        model_revision="qwen3.8-max-0902",
        thinking=False,
        temperature=0,
        top_p=1,
    ),
    "synthesis_action": DevelopmentModelBindingV1(
        capability_alias="synthesis_action",
        provider="alibaba_bailian",
        bifrost_provider="bailian-bridge",
        region="cn-beijing",
        model_revision="qwen3.7-plus-2026-05-26",
        thinking=False,
        temperature=0.3,
        top_p=1,
    ),
    "synthesis_strong": DevelopmentModelBindingV1(
        capability_alias="synthesis_strong",
        provider="alibaba_bailian",
        bifrost_provider="bailian-bridge",
        region="cn-beijing",
        model_revision="qwen3.8-max-0902",
        thinking=False,
        temperature=0,
        top_p=1,
    ),
}

# 受控调用的物理路由引用；`capability_route`落地前用冻结常量，Bridge准入据此核对许可。
ROUTE_REF_PREFIX: Final = "development"


def resolve_development_binding(capability_alias: str) -> DevelopmentModelBindingV1:
    """按能力别名解析冻结开发绑定；未登记即失败关闭。"""
    binding = DEVELOPMENT_BINDINGS.get(capability_alias)
    if binding is None:
        raise ModelBindingViolation("MODEL_BINDING_NOT_REGISTERED")
    return binding


def development_route_ref(capability_alias: str) -> str:
    """`development/<别名>`路由引用；不含端点、凭据或模型名。"""
    binding = resolve_development_binding(capability_alias)
    return f"{ROUTE_REF_PREFIX}/{binding.capability_alias}"


__all__ = [
    "DEVELOPMENT_BINDINGS",
    "ROUTE_REF_PREFIX",
    "DevelopmentModelBindingV1",
    "ModelBindingViolation",
    "development_route_ref",
    "resolve_development_binding",
]
