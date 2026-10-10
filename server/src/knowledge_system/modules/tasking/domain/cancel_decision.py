"""DEV-07 取消请求的纯领域判定：终态不可取消、已取消/已请求取消幂等、开放态可请求。

依据 [关键工程闭环设计集中确认方案] 与 `event_stream.py`：
- 终态（COMPLETED/FAILED/CANCELLED）不可回退，取消终态任务必须以稳定冲突拒绝；
- `TASK_CANCELLED` 是终态事件：服务端记录请求（落事件 + 状态置 `CANCEL_REQUESTED`）后，
  由 Temporal Root 经 `commit_root_terminal_state` 在开放态上 CAS 到 `CANCELLED`；
- 取消请求本身幂等：已 `CANCELLED` 或已 `CANCEL_REQUESTED` 不再重复落事件。

本模块零 I/O、零 LLM，只固化判定；真实 CAS 与 Temporal 收敛属基础设施。
"""

from __future__ import annotations

from enum import StrEnum

from .event_stream import OPEN_STATUSES, TERMINAL_STATUSES

# 可受理取消请求的开放态：`CANCEL_REQUESTED` 单独成行，表示请求已受理、待工作流收敛。
CANCELLABLE_OPEN_STATUSES = OPEN_STATUSES - {"CANCEL_REQUESTED"}


class CancelDisposition(StrEnum):
    """取消请求的确定性判定结果。"""

    REQUESTED = "REQUESTED"
    ALREADY_REQUESTED = "ALREADY_REQUESTED"
    ALREADY_CANCELLED = "ALREADY_CANCELLED"
    TERMINAL = "TERMINAL"


def decide_cancel(status: str) -> CancelDisposition:
    """把任务当前状态折叠为取消请求的处置；未知状态一律按终态拒绝（失败关闭）。"""

    if status == "CANCELLED":
        return CancelDisposition.ALREADY_CANCELLED
    if status == "CANCEL_REQUESTED":
        return CancelDisposition.ALREADY_REQUESTED
    if status in CANCELLABLE_OPEN_STATUSES:
        return CancelDisposition.REQUESTED
    return CancelDisposition.TERMINAL


def is_terminal_status(status: str) -> bool:
    return status in TERMINAL_STATUSES


__all__ = [
    "CANCELLABLE_OPEN_STATUSES",
    "CancelDisposition",
    "decide_cancel",
    "is_terminal_status",
]
