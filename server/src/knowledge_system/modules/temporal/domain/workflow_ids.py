"""DEV-03A：`workflow_id_format_v1`、Signal/Query名称与ID冲突关闭码。

Workflow ID不含环境、用户、团队、标题、模型、时间戳或随机后缀；UUID使用小写标准字符串。
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

ROOT_WORKFLOW_ID_PREFIX: Final = "agent-task-v1-"
ATTEMPT_WORKFLOW_ID_PREFIX: Final = "agent-attempt-v1-"

# Root首版只注册同步Signal与同步只读Query，不注册业务Update或Update-With-Start。
BUSINESS_EVENT_SIGNAL: Final = "business_event_available_v1"
RUNTIME_STATE_QUERY: Final = "runtime_state_v1"

# 既有Task映射/Workflow Type不一致或Child存在而Root无记录时，失败关闭并对账。
TEMPORAL_WORKFLOW_ID_COLLISION: Final = "TEMPORAL_WORKFLOW_ID_COLLISION"


def agent_task_workflow_id(task_id: UUID) -> str:
    """Root Workflow ID：`agent-task-v1-{task_id}`（小写UUID）。"""

    return f"{ROOT_WORKFLOW_ID_PREFIX}{task_id}"


def agent_attempt_workflow_id(task_id: UUID, task_attempt_id: UUID) -> str:
    """Child Workflow ID：`agent-attempt-v1-{task_id}-{task_attempt_id}`。"""

    return f"{ATTEMPT_WORKFLOW_ID_PREFIX}{task_id}-{task_attempt_id}"


__all__ = [
    "ATTEMPT_WORKFLOW_ID_PREFIX",
    "BUSINESS_EVENT_SIGNAL",
    "ROOT_WORKFLOW_ID_PREFIX",
    "RUNTIME_STATE_QUERY",
    "TEMPORAL_WORKFLOW_ID_COLLISION",
    "agent_attempt_workflow_id",
    "agent_task_workflow_id",
]
