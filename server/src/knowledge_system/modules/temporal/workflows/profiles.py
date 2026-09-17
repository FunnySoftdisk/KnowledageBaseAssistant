"""把冻结Activity Profile映射为temporalio配置（DEV-03A契约 -> SDK）。"""

from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

from knowledge_system.modules.temporal.domain.activity_profiles import ActivityProfileV1


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


__all__ = ["heartbeat_timeout", "retry_policy"]
