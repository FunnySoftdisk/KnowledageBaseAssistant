"""Provider错误标准化纯规则。

依据模型网关模块设计§9/§13：把Provider/数据面/网络故障归一为稳定错误码，并区分瞬态依赖错误
（流开始前可重试）与配置/调用方错误（不重试）。稳定code可公开，异常对象不保存输入正文、Secret或
Provider原包。
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ProviderErrorCode(StrEnum):
    # 配置/调用方错误：不重试。
    MODEL_REQUEST_INVALID = "MODEL_REQUEST_INVALID"
    MODEL_ENDPOINT_AUTH_FORBIDDEN = "MODEL_ENDPOINT_AUTH_FORBIDDEN"
    # 项目边界拒绝：不重试，交编排器决定。
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_CAPACITY_EXCEEDED = "MODEL_CAPACITY_EXCEEDED"
    MODEL_CONTEXT_EXCEEDED = "MODEL_CONTEXT_EXCEEDED"
    MODEL_CONTEXT_STALE = "MODEL_CONTEXT_STALE"
    MODEL_CONTEXT_INVALID = "MODEL_CONTEXT_INVALID"
    MODEL_CONTEXT_TOKEN_MISMATCH = "MODEL_CONTEXT_TOKEN_MISMATCH"
    MODEL_STRUCTURED_OUTPUT_INVALID = "MODEL_STRUCTURED_OUTPUT_INVALID"
    MODEL_STREAM_ABORTED = "MODEL_STREAM_ABORTED"
    # 瞬态依赖错误：流开始前可重试。
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    DEPENDENCY_RATE_LIMITED = "DEPENDENCY_RATE_LIMITED"
    DEPENDENCY_TIMEOUT = "DEPENDENCY_TIMEOUT"
    DEPENDENCY_RETRY_AFTER_EXCEEDS_BUDGET = "DEPENDENCY_RETRY_AFTER_EXCEEDS_BUDGET"


class ProviderErrorSource(StrEnum):
    HTTP_STATUS = "HTTP_STATUS"
    CONNECTION = "CONNECTION"
    TIMEOUT = "TIMEOUT"
    STREAM = "STREAM"


class ProviderErrorV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: ProviderErrorCode
    http_status: int | None = None
    source: ProviderErrorSource


# 流开始前可重试的瞬态依赖错误；流开始后统一不自动重试（见retry_policy）。
_TRANSIENT_CODES = frozenset(
    {
        ProviderErrorCode.DEPENDENCY_UNAVAILABLE,
        ProviderErrorCode.DEPENDENCY_RATE_LIMITED,
        ProviderErrorCode.DEPENDENCY_TIMEOUT,
    }
)


def normalize_provider_error(
    *, http_status: int | None = None, source: ProviderErrorSource = ProviderErrorSource.HTTP_STATUS
) -> ProviderErrorV1:
    """把HTTP状态码或传输故障归一到稳定错误码。

    401/403禁用端点并告警、不重试同凭据端点；400为调用方输入错误；429/503/连接失败/超时为
    流开始前可重试的依赖错误；其他4xx按输入错误、其他5xx按依赖不可用处理。`source`非HTTP时
    `http_status`必须为None（非HTTP故障没有状态码）。
    """
    if source is not ProviderErrorSource.HTTP_STATUS:
        if source is ProviderErrorSource.CONNECTION:
            return ProviderErrorV1(
                code=ProviderErrorCode.DEPENDENCY_UNAVAILABLE, source=source
            )
        if source is ProviderErrorSource.TIMEOUT:
            return ProviderErrorV1(code=ProviderErrorCode.DEPENDENCY_TIMEOUT, source=source)
        return ProviderErrorV1(code=ProviderErrorCode.MODEL_STREAM_ABORTED, source=source)

    if http_status in (401, 403):
        return ProviderErrorV1(
            code=ProviderErrorCode.MODEL_ENDPOINT_AUTH_FORBIDDEN,
            http_status=http_status,
            source=source,
        )
    if http_status == 400:
        return ProviderErrorV1(
            code=ProviderErrorCode.MODEL_REQUEST_INVALID, http_status=http_status, source=source
        )
    if http_status == 429:
        return ProviderErrorV1(
            code=ProviderErrorCode.DEPENDENCY_RATE_LIMITED, http_status=http_status, source=source
        )
    if http_status == 503:
        return ProviderErrorV1(
            code=ProviderErrorCode.DEPENDENCY_UNAVAILABLE, http_status=http_status, source=source
        )
    if http_status is not None and 400 <= http_status < 500:
        return ProviderErrorV1(
            code=ProviderErrorCode.MODEL_REQUEST_INVALID, http_status=http_status, source=source
        )
    if http_status is not None and http_status >= 500:
        return ProviderErrorV1(
            code=ProviderErrorCode.DEPENDENCY_UNAVAILABLE, http_status=http_status, source=source
        )
    # 无状态码且无传输类别：保守视为依赖不可用（可对账，不误标失败）。
    return ProviderErrorV1(code=ProviderErrorCode.DEPENDENCY_UNAVAILABLE, source=source)


def is_transient_error(code: ProviderErrorCode) -> bool:
    """瞬态依赖错误：连接失败/503/限流/超时，流开始前可重试。"""
    return code in _TRANSIENT_CODES


def disables_endpoint(code: ProviderErrorCode) -> bool:
    """Provider 401/403：禁用端点并告警，不重试其他同凭据端点。"""
    return code is ProviderErrorCode.MODEL_ENDPOINT_AUTH_FORBIDDEN


__all__ = [
    "ProviderErrorCode",
    "ProviderErrorSource",
    "ProviderErrorV1",
    "disables_endpoint",
    "is_transient_error",
    "normalize_provider_error",
]
