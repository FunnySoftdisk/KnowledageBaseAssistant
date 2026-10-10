"""Bifrost SSE数据面代理端口。

依据模型网关模块设计§4.3：第一版只有面向内部Bifrost HTTP Gateway的Client，Bifrost负责Provider
协议归一与HTTP/SSE转发，项目Policy Adapter负责业务约束、稳定事件、错误与调用账本。端口只暴露
不透明调用/追踪标识与已批准物理路由引用；SecretRef留在项目控制/Broker准入链，不交Bifrost。
完整推理载荷（messages/tools/schema/参数）的请求契约在后续请求建模阶段冻结，本端口不虚构。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from ..domain.stream_event import BifrostEventV1


class BifrostStreamPort(Protocol):
    """把一次已批准的流式调用交给Bifrost数据面，返回原始Bifrost事件流。

    实现位于基础设施层；只允许提交Policy Adapter已批准的路由与不透明标识，禁止接受模型生成的
    Provider、任意base_url或凭据。数据面事件必须经`ProviderEventNormalizer`归一后才可成为
    业务事实；Bifrost中断或统计缺失不得直接修改ModelCall终态。
    """

    async def stream(
        self,
        *,
        model_call_id: str,
        trace_id: str,
        route_ref: str,
    ) -> AsyncIterator[BifrostEventV1]: ...


__all__ = ["BifrostStreamPort"]
