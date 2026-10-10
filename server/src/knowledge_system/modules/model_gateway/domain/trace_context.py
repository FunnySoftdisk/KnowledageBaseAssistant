"""W3C Trace Context 编解码与链路上下文值对象。

依据审计可观测设计§3.5与DEV-04F离线切片：通用Trace与Phoenix LLM Trace沿用同一W3C `trace_id`，
`traceparent` 头按 `version-trace_id-span_id-flags` 编码（仅 version `00`，trace_id 32hex、span_id
16hex、flags 2hex，且 trace_id/span_id 不得全零）。本模块是纯规则，不接OTel SDK、不生成随机ID
（由调用方以32hex生成）；畸形输入以稳定`TRACE_CONTEXT_INVALID`失败关闭，不静默降级。
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

TRACEPARENT_HEADER = "traceparent"
TRACEPARENT_VERSION = "00"

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX16 = re.compile(r"[0-9a-f]{16}")
_HEX2 = re.compile(r"[0-9a-f]{2}")
_ALL_ZERO_32 = "0" * 32
_ALL_ZERO_16 = "0" * 16

TraceId = Annotated[
    str, StringConstraints(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
]
SpanId = Annotated[
    str, StringConstraints(min_length=16, max_length=16, pattern=r"^[0-9a-f]{16}$")
]


class TraceContextViolation(ValueError):
    """稳定code可公开；异常中不保存Trace正文、凭据或Secret。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SpanContextV1(BaseModel):
    """链路上下文；`trace_id`贯穿整条链路，`span_id`为当前/上一跳Span，`sampled`为W3C采样位。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    trace_id: TraceId
    span_id: SpanId
    sampled: bool = False


def encode_traceparent(context: SpanContextV1) -> str:
    """把链路上下文编码为`traceparent`头；`span_id`作为子Span的parent。"""
    flags = "01" if context.sampled else "00"
    return f"{TRACEPARENT_VERSION}-{context.trace_id}-{context.span_id}-{flags}"


def decode_traceparent(value: str) -> SpanContextV1:
    """解析`traceparent`头；格式/版本/长度/全零校验失败均抛`TRACE_CONTEXT_INVALID`。"""
    parts = value.split("-")
    if len(parts) != 4:
        raise TraceContextViolation("TRACE_CONTEXT_INVALID")
    version, trace_id, span_id, flags = parts
    if version != TRACEPARENT_VERSION:
        raise TraceContextViolation("TRACE_CONTEXT_INVALID")
    if not _HEX32.fullmatch(trace_id) or trace_id == _ALL_ZERO_32:
        raise TraceContextViolation("TRACE_CONTEXT_INVALID")
    if not _HEX16.fullmatch(span_id) or span_id == _ALL_ZERO_16:
        raise TraceContextViolation("TRACE_CONTEXT_INVALID")
    if not _HEX2.fullmatch(flags):
        raise TraceContextViolation("TRACE_CONTEXT_INVALID")
    sampled = (int(flags, 16) & 0x01) == 1
    return SpanContextV1(trace_id=trace_id, span_id=span_id, sampled=sampled)


__all__ = [
    "TRACEPARENT_HEADER",
    "TRACEPARENT_VERSION",
    "SpanContextV1",
    "SpanId",
    "TraceContextViolation",
    "TraceId",
    "decode_traceparent",
    "encode_traceparent",
]
