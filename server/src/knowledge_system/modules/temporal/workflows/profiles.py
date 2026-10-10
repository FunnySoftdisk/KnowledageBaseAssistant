"""把冻结Activity Profile映射为temporalio配置（DEV-03A契约 -> SDK）。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio.common import RetryPolicy

from knowledge_system.modules.temporal.domain.activity_profiles import (
    ActivityProfileKindV1,
    ActivityProfileV1,
    activity_profile,
)


def retry_policy(profile: ActivityProfileV1) -> RetryPolicy | None:
    """不重试的Profile（`retry=None`）返回None；`maximum_attempts`包含首次执行。"""

    if profile.retry is None:
        return None
    return RetryPolicy(
        initial_interval=timedelta(seconds=profile.retry.initial_interval_seconds),
        backoff_coefficient=profile.retry.backoff_coefficient,
        maximum_interval=timedelta(seconds=profile.retry.max_interval_seconds),
        maximum_attempts=profile.max_attempts,
    )


def heartbeat_timeout(profile: ActivityProfileV1) -> timedelta | None:
    if profile.heartbeat_seconds is None:
        return None
    return timedelta(seconds=profile.heartbeat_seconds)


def activity_execute_kwargs(
    kind: ActivityProfileKindV1, task_queue: str
) -> dict[str, Any]:
    """把一个Activity Profile映射为`execute_activity`关键字参数。

    Activity必须显式指定Task Queue（禁止继承Workflow Queue）；`retry_policy=None`表示
    该Profile不重试。Root与Child共用此映射，避免各自手写超时/退避散落。
    """

    profile = activity_profile(kind)
    return {
        "task_queue": task_queue,
        "start_to_close_timeout": timedelta(seconds=profile.start_to_close_seconds),
        "schedule_to_close_timeout": timedelta(seconds=profile.schedule_to_close_seconds),
        "heartbeat_timeout": heartbeat_timeout(profile),
        "retry_policy": retry_policy(profile),
    }


__all__ = ["activity_execute_kwargs", "heartbeat_timeout", "retry_policy"]
