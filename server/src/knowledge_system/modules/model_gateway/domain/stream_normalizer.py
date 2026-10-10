"""Bifrost SSE数据面事件到项目稳定`ModelEvent`的归一与终态解析。

依据模型网关模块设计§7/§12/§13：Provider重复帧按`chunk_id`去重、序号由网关单调生成、首个实质
Delta记录TTFT并进入`STREAMING`、最终Usage缺失时标记估算（不伪装Provider精确值）、流开始后依赖
失败归一为`MODEL_STREAM_ABORTED`且不透明切副本、网络结果未知不得误标失败（留待按Provider
Request ID对账）。本模块是纯规则，不持有时钟、不接网络；TTFT由上层按首个Delta落秒记录。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .call_state import ModelCallState
from .provider_error import (
    ProviderErrorCode,
    ProviderErrorSource,
    ProviderErrorV1,
    normalize_provider_error,
)
from .stream_event import (
    BifrostDeltaEventV1,
    BifrostErrorEventV1,
    BifrostEventV1,
    BifrostUsageEventV1,
    ModelDeltaEventV1,
    ModelErrorEventV1,
    ModelEventV1,
    ModelStartedEventV1,
    ModelUsageEventV1,
)
from .usage import (
    EstimatedModelUsageV1,
    ExactModelUsageV1,
    ModelUsageV1,
    UnknownModelUsageV1,
)


class StreamNormalizerState(BaseModel):
    """归一器累计状态；`sequence`为首个事件序号前值（初始0），发出事件时自增。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sequence: int = 0
    stream_started: bool = False
    finish_reason: str | None = None
    provider_usage: ExactModelUsageV1 | None = None
    seen_chunk_ids: frozenset[str] = frozenset()


def normalize_bifrost_event(
    event: BifrostEventV1,
    state: StreamNormalizerState,
) -> tuple[StreamNormalizerState, list[ModelEventV1]]:
    """把单个Bifrost数据面事件归一为0..n个稳定`ModelEvent`，返回新状态与事件列表。"""
    if isinstance(event, BifrostDeltaEventV1):
        return _normalize_delta(event, state)
    if isinstance(event, BifrostUsageEventV1):
        return _normalize_usage(event, state)
    return _normalize_error(event, state)


def _normalize_delta(
    event: BifrostDeltaEventV1,
    state: StreamNormalizerState,
) -> tuple[StreamNormalizerState, list[ModelEventV1]]:
    if event.chunk_id is not None and event.chunk_id in state.seen_chunk_ids:
        return state, []

    events: list[ModelEventV1] = []
    sequence = state.sequence
    stream_started = state.stream_started
    finish_reason = state.finish_reason

    if event.finish_reason is not None:
        finish_reason = event.finish_reason

    if event.text_delta:
        if not stream_started:
            sequence += 1
            events.append(ModelStartedEventV1(type="STARTED", sequence=sequence))
            stream_started = True
        sequence += 1
        events.append(
            ModelDeltaEventV1(
                type="DELTA",
                sequence=sequence,
                text_delta=event.text_delta,
                finish_reason=event.finish_reason,
            )
        )

    seen = state.seen_chunk_ids
    if event.chunk_id is not None:
        seen = seen | {event.chunk_id}

    new_state = StreamNormalizerState(
        sequence=sequence,
        stream_started=stream_started,
        finish_reason=finish_reason,
        provider_usage=state.provider_usage,
        seen_chunk_ids=seen,
    )
    return new_state, events


def _normalize_usage(
    event: BifrostUsageEventV1,
    state: StreamNormalizerState,
) -> tuple[StreamNormalizerState, list[ModelEventV1]]:
    usage = ExactModelUsageV1(
        kind="EXACT",
        input_tokens=event.input_tokens,
        output_tokens=event.output_tokens,
        source="PROVIDER",
    )
    sequence = state.sequence + 1
    new_state = StreamNormalizerState(
        sequence=sequence,
        stream_started=state.stream_started,
        finish_reason=state.finish_reason,
        provider_usage=usage,
        seen_chunk_ids=state.seen_chunk_ids,
    )
    return new_state, [ModelUsageEventV1(type="USAGE", sequence=sequence, usage=usage)]


def _normalize_error(
    event: BifrostErrorEventV1,
    state: StreamNormalizerState,
) -> tuple[StreamNormalizerState, list[ModelEventV1]]:
    error = normalize_provider_error(http_status=event.http_status, source=event.source)
    if state.stream_started:
        # 流开始后依赖失败：不透明切副本继续以免文本重复/矛盾，统一归为MODEL_STREAM_ABORTED。
        error = ProviderErrorV1(
            code=ProviderErrorCode.MODEL_STREAM_ABORTED,
            source=ProviderErrorSource.STREAM,
        )
    sequence = state.sequence + 1
    new_state = StreamNormalizerState(
        sequence=sequence,
        stream_started=state.stream_started,
        finish_reason=state.finish_reason,
        provider_usage=state.provider_usage,
        seen_chunk_ids=state.seen_chunk_ids,
    )
    return new_state, [ModelErrorEventV1(type="ERROR", sequence=sequence, error=error)]


class StreamTerminalV1(BaseModel):
    """一次流式调用结束时的ModelCall终态与结算Usage。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: ModelCallState
    error_code: str | None = None
    usage: ModelUsageV1


def resolve_stream_terminal(
    *,
    state: StreamNormalizerState,
    completed: bool,
    error: ProviderErrorV1 | None = None,
    cancelled: bool = False,
    estimated_input_tokens: int | None = None,
    estimated_output_tokens: int | None = None,
) -> StreamTerminalV1:
    """把一次已结束的流式调用解析为ModelCall终态与结算Usage。

    `completed`表示Bifrost数据面流干净结束（迭代器耗尽且无错误），由上层据实际EOF判定。终态Usage
    优先Provider实测（EXACT），缺失时用预留最坏值估算（ESTIMATED_DEV），两者皆无记UNKNOWN。
    """
    usage = _resolve_usage(
        state.provider_usage, estimated_input_tokens, estimated_output_tokens
    )

    if cancelled:
        return StreamTerminalV1(status=ModelCallState.CANCELLED, usage=usage)

    if error is not None:
        if state.stream_started:
            return StreamTerminalV1(
                status=ModelCallState.FAILED,
                error_code=ProviderErrorCode.MODEL_STREAM_ABORTED.value,
                usage=usage,
            )
        if error.source in (ProviderErrorSource.CONNECTION, ProviderErrorSource.TIMEOUT):
            # 网络结果未知：不误标失败，留待按Provider Request ID对账。
            return StreamTerminalV1(status=ModelCallState.UNKNOWN, usage=usage)
        return StreamTerminalV1(
            status=ModelCallState.FAILED, error_code=error.code.value, usage=usage
        )

    if completed:
        return StreamTerminalV1(status=ModelCallState.COMPLETED, usage=usage)

    if state.stream_started:
        # 已产生Delta但未完成也未报错：流截断。
        return StreamTerminalV1(
            status=ModelCallState.FAILED,
            error_code=ProviderErrorCode.MODEL_STREAM_ABORTED.value,
            usage=usage,
        )
    return StreamTerminalV1(status=ModelCallState.UNKNOWN, usage=usage)


def _resolve_usage(
    provider_usage: ExactModelUsageV1 | None,
    estimated_input_tokens: int | None,
    estimated_output_tokens: int | None,
) -> ModelUsageV1:
    if provider_usage is not None:
        return provider_usage
    if estimated_input_tokens is not None and estimated_output_tokens is not None:
        return EstimatedModelUsageV1(
            kind="ESTIMATED_DEV",
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            source="WORST_CASE_RESERVATION",
        )
    return UnknownModelUsageV1(kind="UNKNOWN")


__all__ = [
    "StreamNormalizerState",
    "StreamTerminalV1",
    "normalize_bifrost_event",
    "resolve_stream_terminal",
]
