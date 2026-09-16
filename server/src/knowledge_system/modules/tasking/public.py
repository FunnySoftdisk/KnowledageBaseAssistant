"""任务入口对外公开接口；API层与测试只引用本模块。"""

from .application.errors import TaskEntryError
from .application.gateway import (
    AttachmentEntrySnapshot,
    ConversationEntrySnapshot,
    TaskEntryGateway,
    TaskQueryGateway,
    TaskRowSnapshot,
)
from .application.task_creation import CreateTaskResult, TaskCreationService
from .application.task_queries import TaskDetailView, TaskQueryService, TaskResultView
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
    "TaskCreationService",
    "TaskDetailView",
    "TaskEntryError",
    "TaskEntryGateway",
    "TaskQueryGateway",
    "TaskQueryService",
    "TaskResultView",
    "TaskRowSnapshot",
    "create_task_request_digest",
    "parse_create_task_request",
]
