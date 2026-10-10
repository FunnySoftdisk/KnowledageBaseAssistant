"""流开始前后不同重试策略的纯规则。

依据模型网关模块设计§9：模型调用只由`MODEL_REQUEST` Activity最多执行2次并沿用同一`model_call_id`；
PydanticAI/Bifrost/Provider自动重试固定为0，重试权只在Temporal层。连接失败/503/限流且未产生Delta时
返回可重试依赖错误；429/503解析`Retry-After`精确覆盖普通退避且不附加抖动，超过边界则停止当前
Activity重试；400/401/403/Schema输入错误不重试；流开始后不自动重试。普通瞬态错误退避
5秒起步、系数2.0、最大30秒，附加按`activity_id + attempt`哈希的±20%确定性抖动。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum

from .provider_error import ProviderErrorCode, is_transient_error

MAX_RETRY_AFTER_SECONDS = 60
_BASE_BACKOFF_SECONDS = 5.0
_BACKOFF_COEFFICIENT = 2.0
_MAX_BACKOFF_SECONDS = 30.0
_JITTER_FRACTION = 0.2


class RetryOutcome(StrEnum):
    RETRY = "RETRY"
    NO_RETRY = "NO_RETRY"


class RetryDelayDecision(StrEnum):
    RETRY_AFTER_EXCEEDS_BUDGET = "DEPENDENCY_RETRY_AFTER_EXCEEDS_BUDGET"


def classify_retry(code: ProviderErrorCode, *, stream_started: bool) -> RetryOutcome:
    """流开始后不自动重试；流开始前仅瞬态依赖错误可重试，配置/调用方错误不重试。"""
    if stream_started:
        return RetryOutcome.NO_RETRY
    if is_transient_error(code):
        return RetryOutcome.RETRY
    return RetryOutcome.NO_RETRY


def parse_retry_after(value: str | None) -> int | None:
    """解析`Retry-After`为delta-seconds或HTTP-date，返回非负秒数；缺失/非法返回None。

    只接受整数delta-seconds或RFC 7231 HTTP-date；负数、超范围或无法解析均视为无效，
    由调用方退回普通退避。
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.isdigit():
        return int(stripped)
    try:
        parsed = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta = (parsed - datetime.now(UTC)).total_seconds()
    if delta < 0:
        return None
    return int(delta)


def compute_next_retry_delay(
    *,
    attempt: int,
    activity_id: str,
    retry_after_seconds: int | None = None,
    remaining_schedule_to_close: float | None = None,
) -> int | RetryDelayDecision:
    """返回下一次Activity尝试的退避秒数；Retry-After超边界返回`RETRY_AFTER_EXCEEDS_BUDGET`。

    `attempt`从1起；`retry_after_seconds`为合法非负值时精确覆盖且不附加抖动；普通退避为
    `min(5*2^(attempt-1), 30)`秒并叠加±20%确定性抖动。
    """
    if type(attempt) is not int or attempt < 1:
        raise ValueError("attempt must be a positive integer")

    if retry_after_seconds is not None:
        if type(retry_after_seconds) is not int or retry_after_seconds < 0:
            return _default_backoff(attempt, activity_id)
        if retry_after_seconds > MAX_RETRY_AFTER_SECONDS:
            return RetryDelayDecision.RETRY_AFTER_EXCEEDS_BUDGET
        if (
            remaining_schedule_to_close is not None
            and retry_after_seconds > remaining_schedule_to_close
        ):
            return RetryDelayDecision.RETRY_AFTER_EXCEEDS_BUDGET
        return retry_after_seconds

    return _default_backoff(attempt, activity_id)


def _default_backoff(attempt: int, activity_id: str) -> int:
    base = min(
        _BASE_BACKOFF_SECONDS * (_BACKOFF_COEFFICIENT ** (attempt - 1)),
        _MAX_BACKOFF_SECONDS,
    )
    multiplier = _jitter_multiplier(activity_id, attempt)
    return max(0, round(base * multiplier))


def _jitter_multiplier(activity_id: str, attempt: int) -> float:
    """按`activity_id + attempt`的SHA-256映射到[1-0.2, 1+0.2]的确定性抖动。"""
    digest = hashlib.sha256(f"{activity_id}:{attempt}".encode()).digest()
    unit = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    return 1.0 + (unit - 0.5) * 2.0 * _JITTER_FRACTION


__all__ = [
    "MAX_RETRY_AFTER_SECONDS",
    "RetryDelayDecision",
    "RetryOutcome",
    "classify_retry",
    "compute_next_retry_delay",
    "parse_retry_after",
]
