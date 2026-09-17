"""DEV-03A：八类Activity执行Profile与重试分类（冻结数值）。

时间单位均为秒。最大尝试次数包含首次执行；`retry=None`表示不重试
（`CAPABILITY_EFFECT_UNCERTAIN`）。首版所有Agent Activity不设置Schedule-to-Start Timeout，
Workflow不设置Retry Policy。普通退避的确定性抖动与`Retry-After`上限由Activity在
`next_retry_delay`侧实现，不进本模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class ActivityProfileKindV1(StrEnum):
    MODEL_REQUEST = "MODEL_REQUEST"
    CAPABILITY_DISCOVERY = "CAPABILITY_DISCOVERY"
    CAPABILITY_READ = "CAPABILITY_READ"
    CAPABILITY_EFFECT_IDEMPOTENT = "CAPABILITY_EFFECT_IDEMPOTENT"
    CAPABILITY_EFFECT_UNCERTAIN = "CAPABILITY_EFFECT_UNCERTAIN"
    CONTROL_TRANSACTION = "CONTROL_TRANSACTION"
    EVENT_STREAM_WRITE = "EVENT_STREAM_WRITE"
    RESUME_BUNDLE_IO = "RESUME_BUNDLE_IO"


@dataclass(frozen=True, slots=True)
class RetryProfileV1:
    """退避曲线；`retry_after_cap_seconds=None`表示该Profile不适用Retry-After上限。"""

    initial_interval_seconds: float
    backoff_coefficient: float
    max_interval_seconds: float
    retry_after_cap_seconds: float | None


@dataclass(frozen=True, slots=True)
class ActivityProfileV1:
    kind: ActivityProfileKindV1
    start_to_close_seconds: float
    schedule_to_close_seconds: float
    heartbeat_seconds: float | None
    max_attempts: int
    retry: RetryProfileV1 | None


ACTIVITY_PROFILES_V1: Final[dict[ActivityProfileKindV1, ActivityProfileV1]] = {
    ActivityProfileKindV1.MODEL_REQUEST: ActivityProfileV1(
        ActivityProfileKindV1.MODEL_REQUEST, 300, 600, 30, 2, RetryProfileV1(5, 2.0, 30, 60)
    ),
    ActivityProfileKindV1.CAPABILITY_DISCOVERY: ActivityProfileV1(
        ActivityProfileKindV1.CAPABILITY_DISCOVERY, 15, 45, None, 3, RetryProfileV1(1, 2.0, 5, 10)
    ),
    ActivityProfileKindV1.CAPABILITY_READ: ActivityProfileV1(
        ActivityProfileKindV1.CAPABILITY_READ, 180, 600, 30, 3, RetryProfileV1(2, 2.0, 20, 30)
    ),
    ActivityProfileKindV1.CAPABILITY_EFFECT_IDEMPOTENT: ActivityProfileV1(
        ActivityProfileKindV1.CAPABILITY_EFFECT_IDEMPOTENT,
        180,
        600,
        30,
        3,
        RetryProfileV1(5, 2.0, 30, 60),
    ),
    ActivityProfileKindV1.CAPABILITY_EFFECT_UNCERTAIN: ActivityProfileV1(
        ActivityProfileKindV1.CAPABILITY_EFFECT_UNCERTAIN, 180, 240, 30, 1, None
    ),
    ActivityProfileKindV1.CONTROL_TRANSACTION: ActivityProfileV1(
        ActivityProfileKindV1.CONTROL_TRANSACTION, 15, 60, None, 5, RetryProfileV1(1, 2.0, 8, None)
    ),
    ActivityProfileKindV1.EVENT_STREAM_WRITE: ActivityProfileV1(
        ActivityProfileKindV1.EVENT_STREAM_WRITE, 10, 30, None, 3, RetryProfileV1(1, 2.0, 4, None)
    ),
    ActivityProfileKindV1.RESUME_BUNDLE_IO: ActivityProfileV1(
        ActivityProfileKindV1.RESUME_BUNDLE_IO, 30, 120, None, 5, RetryProfileV1(1, 2.0, 10, None)
    ),
}


def activity_profile(kind: ActivityProfileKindV1) -> ActivityProfileV1:
    return ACTIVITY_PROFILES_V1[kind]


__all__ = [
    "ACTIVITY_PROFILES_V1",
    "ActivityProfileKindV1",
    "ActivityProfileV1",
    "RetryProfileV1",
    "activity_profile",
]
