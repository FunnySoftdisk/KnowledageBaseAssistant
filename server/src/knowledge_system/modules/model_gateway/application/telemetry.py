"""OTel/Phoenix遥测端口（DEV-04F离线切片）。

依据审计可观测设计§3.5：`TelemetryService`是业务代码对OpenTelemetry的唯一抽象，后端可替换、不把
业务绑定某个AI Trace产品。本端口只冻结签名；`traceparent`编解码见`domain/trace_context.py`，
Span属性白名单/Secret剥离见`domain/span_policy.py`。OTel SDK埋点与Collector/Phoenix接入属基础
设施与真实部署，留待EXTERNAL_REQUIRED。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from ..domain.trace_context import SpanContextV1


class TelemetryService(Protocol):
    """开始一个Span并返回其上下文；`attributes`须经`span_policy`白名单/脱敏后再传入。"""

    def start_span(self, name: str, attributes: Mapping[str, object]) -> SpanContextV1: ...

    def record_metric(self, name: str, value: float, attributes: Mapping[str, object]) -> None: ...

    def inject_context(self, carrier: dict[str, str]) -> None: ...

    def extract_context(self, carrier: Mapping[str, str]) -> SpanContextV1: ...


__all__ = ["TelemetryService"]
