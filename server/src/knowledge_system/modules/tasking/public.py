"""任务入口对外公开接口；API层与测试只引用本模块。"""

from .application.errors import TaskEntryError
from .application.gateway import (
    AttachmentEntrySnapshot,
    ConversationEntrySnapshot,
    TaskEntryGateway,
    TaskEventSnapshot,
    TaskQueryGateway,
    TaskRowSnapshot,
)
from .application.task_cancel import TaskCancellationService, TaskCancelResult
from .application.task_creation import CreateTaskResult, TaskCreationService
from .application.task_events import TaskEventReconnectResultV1, TaskEventStreamService
from .application.task_queries import TaskDetailView, TaskQueryService, TaskResultView
from .domain.event_stream import SSEStreamError, TaskEventProjectionV1
from .domain.input_contracts import (
    CreateTaskRequestV1,
    create_task_request_digest,
    parse_create_task_request,
)

__all__ = [
    "AttachmentEntrySnapshot",
    "ConversationEntrySnapshot",
    "CreateTaskRequestV1",
    "CreateTaskResult",
    "TaskCancelResult",
    "TaskCancellationService",
    "TaskCreationService",
    "TaskDetailView",
    "TaskEntryError",
    "TaskEntryGateway",
    "TaskEventProjectionV1",
    "TaskEventReconnectResultV1",
    "TaskEventSnapshot",
    "TaskEventStreamService",
    "TaskQueryGateway",
    "TaskQueryService",
    "TaskResultView",
    "TaskRowSnapshot",
    "create_task_request_digest",
    "parse_create_task_request",
    "SSEStreamError",
]
