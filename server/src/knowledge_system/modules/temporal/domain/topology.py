"""DEV-03A：五个Task Queue、Worker Deployment/Build ID、Slot与Search Attribute。

队列名不携带发布版本，`.v1`只表示队列契约主版本。所有Activity必须显式指定Task Queue，
禁止继承Workflow Queue后意外执行。Namespace按环境固定隔离，不按用户/团队/租户创建。
"""

from __future__ import annotations

from typing import Final

WORKFLOW_TASK_QUEUE: Final = "knowledge.agent.workflow.v1"
MODEL_TASK_QUEUE: Final = "knowledge.agent.model.v1"
CAPABILITY_TASK_QUEUE: Final = "knowledge.agent.capability.v1"
CONTROL_TASK_QUEUE: Final = "knowledge.agent.control.v1"
BUNDLE_TASK_QUEUE: Final = "knowledge.agent.bundle.v1"

AGENT_TASK_QUEUES: Final[tuple[str, ...]] = (
    WORKFLOW_TASK_QUEUE,
    MODEL_TASK_QUEUE,
    CAPABILITY_TASK_QUEUE,
    CONTROL_TASK_QUEUE,
    BUNDLE_TASK_QUEUE,
)

WORKER_DEPLOYMENT: Final = "knowledge-agent-runtime"

# 五池首版使用FixedSizeSlotSupplier，每副本固定Slot：workflow/model/capability/control/bundle。
SLOT_SUPPLIERS: Final[dict[str, int]] = {
    WORKFLOW_TASK_QUEUE: 100,
    MODEL_TASK_QUEUE: 8,
    CAPABILITY_TASK_QUEUE: 16,
    CONTROL_TASK_QUEUE: 16,
    BUNDLE_TASK_QUEUE: 4,
}

# Root/Child Worker Versioning Behavior均为PINNED。
WORKER_VERSIONING_BEHAVIOR: Final = "PINNED"

# Root/Child均不设Execution/Run Timeout；每次Workflow Task明确限10秒。
WORKFLOW_TASK_TIMEOUT_SECONDS: Final = 10

WORKFLOW_KIND_ROOT: Final = "ROOT"
WORKFLOW_KIND_ATTEMPT: Final = "ATTEMPT"


def build_id(release_version: str, git_sha: str) -> str:
    """Worker Build ID：`{release_version}+{git_sha}`；五个Worker池以同一Build ID发布。"""

    return f"{release_version}+{git_sha}"


# `temporal_search_attributes_v1`：八个低敏类型化字段（name -> Temporal类型）。
# Visibility最终一致且仅供运维发现，业务真相仍是PG；不使用Memo，不写正文/PII/Hash/Secret。
SEARCH_ATTRIBUTES: Final[dict[str, str]] = {
    "AgentRuntimeContract": "Keyword",
    "AgentWorkflowKind": "Keyword",
    "AgentRunState": "Keyword",
    "AgentAttemptKind": "Keyword",
    "AgentRegistrationKey": "Keyword",
    "AgentDeadline": "Datetime",
    "AgentNeedsReconcile": "Bool",
    "AgentRunSequence": "Int",
}


__all__ = [
    "AGENT_TASK_QUEUES",
    "BUNDLE_TASK_QUEUE",
    "CAPABILITY_TASK_QUEUE",
    "CONTROL_TASK_QUEUE",
    "MODEL_TASK_QUEUE",
    "SEARCH_ATTRIBUTES",
    "SLOT_SUPPLIERS",
    "WORKER_DEPLOYMENT",
    "WORKER_VERSIONING_BEHAVIOR",
    "WORKFLOW_TASK_TIMEOUT_SECONDS",
    "WORKFLOW_KIND_ATTEMPT",
    "WORKFLOW_KIND_ROOT",
    "WORKFLOW_TASK_QUEUE",
    "build_id",
]
