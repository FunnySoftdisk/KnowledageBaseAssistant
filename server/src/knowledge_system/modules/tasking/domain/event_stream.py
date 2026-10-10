"""DEV-07 后端离线切片：Task Event 游标、SSE 重连、终态查询与断线恢复的纯领域契约。

依据[关键工程闭环设计集中确认方案]§"REST读取PG已提交Task/Result才是业务事实"与 G13-E09：
- SSE 只发送 `workflow.task_event` 的**授权投影**，按持久化 `sequence` 续读
  （`Last-Event-ID` 游标），不新建一套 SSE 状态机，不传播内部 Outbox payload /
  内部 Ref / Digest / 正文 / Secret；
- 终态（COMPLETED/FAILED/CANCELLED）以 REST 读取已提交结果为准；断开 SSE 不是取消；
- 失败与取消**绝不**显示为成功。

本模块零 I/O、零 LLM，只固化：任务状态枚举、SSE 游标值对象、可流式事件白名单与授权投影、
以及「断线后先查终态、再按游标续读」的重连判定。SSE 长连接、心跳与 REST 端点属基础设施，后续接入。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .core_types import StrictContractV1

# 任务状态全集（`workflow.intelligent_task.status` CHECK 约束的合法值）。
_TASK_STATUS_VALUES = (
    "QUEUED",
    "PLANNING",
    "RUNNING",
    "REPLANNING",
    "FINALIZING",
    "WAITING_USER_INPUT",
    "WAITING_APPROVAL",
    "RETRY_WAIT",
    "CANCEL_REQUESTED",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
)

# 终态：REST 读取已提交结果后即可渲染，SSE 不再推进。
TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

# 开放态：仍可能有事件推进，重连后按游标续读。
OPEN_STATUSES = frozenset(_TASK_STATUS_VALUES) - TERMINAL_STATUSES

# 可流式到桌面的任务生命周期事件白名单（失败关闭：未列出的内部事件仍持久化、但不投影）。
SSE_STREAMABLE_EVENT_TYPES = frozenset(
    {
        "TASK_CREATED",
        "GOAL_UNDERSTANDING_COMPLETED",
        "PLAN_ACTIVATED",
        "USER_INPUT_REQUESTED",
        "TASK_FAILED",
        "TASK_CANCELLED",
    }
)

# SSE 心跳注释（打开但暂无新事件时由服务端按周期发送，保持连接与代理存活）。
SSE_HEARTBEAT_COMMENT = "ping"

# 授权投影 payload 的禁用字段名（规范化小写）：Secret/认证材料与内部摘要不得进入流式负载。
_FORBIDDEN_PROJECTION_KEYS = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "api_key",
        "apikey",
        "x_api_key",
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "bearer",
        "password",
        "passwd",
        "secret",
        "secret_ref",
        "secretref",
        "credential",
        "credentials",
        "private_key",
        "signing_key",
        "jwt",
    }
)


class TaskStatusV1(StrEnum):
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    REPLANNING = "REPLANNING"
    FINALIZING = "FINALIZING"
    WAITING_USER_INPUT = "WAITING_USER_INPUT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RETRY_WAIT = "RETRY_WAIT"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self.value in TERMINAL_STATUSES

    @property
    def is_open(self) -> bool:
        return self.value in OPEN_STATUSES


class TerminalDisplayV1(StrEnum):
    """终态在桌面端的展示结论：只有 COMPLETED 显示成功，失败/取消绝不冒充成功。"""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RecoverActionV1(StrEnum):
    RENDER_RESULT = "RENDER_RESULT"
    STREAM_RESUME = "STREAM_RESUME"


class SSEStreamError(ValueError):
    """稳定 code 可公开；异常不携带 Secret、payload 正文或内部 Ref。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TaskEventCursorV1(StrictContractV1):
    """SSE 续读游标：`sequence=0` 表示尚无先验事件（首连），否则为已见最后事件序号。

    `Last-Event-ID` 语义：服务端从 `resume_from_sequence = sequence + 1`（不含）开始续读。
    """

    sequence: int = Field(ge=0)

    @property
    def resume_from_sequence(self) -> int:
        return self.sequence + 1


class TaskProgressProjectionV1(BaseModel):
    """可流式事件的授权负载投影：只保留低基数业务进度事实。

    `payload_json` 经 JSON 反序列化为 JSON 原语（str/number/bool/null），故关闭 `strict` 以允许
    str→UUID/枚举的 JSON 往返（沿 `TemporalContractV1` 先例），但保留 `extra=forbid`：任何内部
    字段（Ref/Digest/正文/Provider 原包）进入即失败关闭；这是白名单投影，不是放行其余。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID | None = None
    status: TaskStatusV1 | None = None
    plan_version: int | None = Field(default=None, ge=1)
    reason_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    display_summary: str | None = Field(default=None, max_length=512)


class TaskEventProjectionV1(StrictContractV1):
    """单条可流式事件的 SSE 信封：`sequence` 即 SSE `id:`，负载为授权投影。"""

    sequence: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    event_schema_version: str = Field(min_length=1, max_length=64)
    payload: TaskProgressProjectionV1


class ReconnectDecisionV1(StrictContractV1):
    """断线重连的确定性判定：先查终态、再按游标续读；失败关闭的协议异常直接抛错。"""

    action: RecoverActionV1
    terminal_display: TerminalDisplayV1 | None = None
    resume_from_sequence: int | None = Field(default=None, ge=1)
    heartbeat_required: bool = False

    @model_validator(mode="after")
    def _action_shape(self) -> ReconnectDecisionV1:
        if self.action is RecoverActionV1.RENDER_RESULT:
            if self.terminal_display is None or self.resume_from_sequence is not None:
                raise ValueError("SSE_RENDER_RESULT_SHAPE_INVALID")
            if self.heartbeat_required:
                raise ValueError("SSE_RENDER_RESULT_NO_HEARTBEAT")
        else:
            if self.resume_from_sequence is None or self.terminal_display is not None:
                raise ValueError("SSE_STREAM_RESUME_SHAPE_INVALID")
        return self


def parse_event_cursor(raw: str | None) -> TaskEventCursorV1:
    """解析 SSE `Last-Event-ID`；缺省/空串为 `sequence=0`（首连），非法值失败关闭。

    只接受十进制非负整数，越界（>2^63-1）或负值/非数字一律 `SSE_CURSOR_INVALID`。
    """

    if raw is None or raw.strip() == "":
        return TaskEventCursorV1(sequence=0)
    token = raw.strip()
    if not token.isascii() or not token.isdigit():
        raise SSEStreamError("SSE_CURSOR_INVALID")
    try:
        value = int(token)
    except ValueError as error:
        raise SSEStreamError("SSE_CURSOR_INVALID") from error
    if value < 0 or value > 2**63 - 1:
        raise SSEStreamError("SSE_CURSOR_INVALID")
    return TaskEventCursorV1(sequence=value)


def terminal_display_for(status: TaskStatusV1) -> TerminalDisplayV1:
    """终态 → 桌面展示结论；只有 COMPLETED 是 SUCCESS，失败/取消绝不显示为成功。"""

    if status is TaskStatusV1.COMPLETED:
        return TerminalDisplayV1.SUCCESS
    if status is TaskStatusV1.FAILED:
        return TerminalDisplayV1.FAILED
    if status is TaskStatusV1.CANCELLED:
        return TerminalDisplayV1.CANCELLED
    raise SSEStreamError("SSE_TERMINAL_STATUS_REQUIRED")


def is_forbidden_projection_key(name: str) -> bool:
    """负载字段名是否命中 Secret/认证材料或内部摘要（大小写不敏感）。"""

    key = name.strip().lower()
    return key in _FORBIDDEN_PROJECTION_KEYS or key.endswith(("_digest", "_sha256"))


def project_stream_event(
    *,
    sequence: int,
    event_type: str,
    event_schema_version: str,
    payload: Mapping[str, object],
) -> TaskEventProjectionV1 | None:
    """把一条 `task_event` 行投影为桌面 SSE 信封；非可流式类型返回 `None`（静默不投影）。

    - 未列入 `SSE_STREAMABLE_EVENT_TYPES` 的内部事件：持久化但不出桌面流；
    - 可流式事件负载命中禁用字段名 → `SSE_FORBIDDEN_PAYLOAD_FIELD` 失败关闭；
    - 可流式事件负载含白名单之外字段 → `SSE_PROJECTION_FIELD_NOT_ALLOWED` 失败关闭。
    """

    if event_type not in SSE_STREAMABLE_EVENT_TYPES:
        return None
    for name in payload:
        if is_forbidden_projection_key(name):
            raise SSEStreamError("SSE_FORBIDDEN_PAYLOAD_FIELD")
    try:
        projection = TaskProgressProjectionV1.model_validate(dict(payload))
    except ValidationError as error:
        raise SSEStreamError("SSE_PROJECTION_FIELD_NOT_ALLOWED") from error
    return TaskEventProjectionV1(
        sequence=sequence,
        event_type=event_type,
        event_schema_version=event_schema_version,
        payload=projection,
    )


def decide_reconnect(
    *,
    status: TaskStatusV1,
    server_last_event_sequence: int,
    cursor: TaskEventCursorV1,
) -> ReconnectDecisionV1:
    """断线恢复判定：终态先渲染结果、开放态按游标续读；游标越界失败关闭。

    - 终态：`RENDER_RESULT`，展示结论由 `terminal_display_for` 给出（失败/取消≠成功）；
    - 开放态：`STREAM_RESUME`，从 `cursor + 1` 续读；客户端已见序号超过服务端水位视为协议异常
      （`SSE_CURSOR_AHEAD_OF_SERVER`），客户端必须先以 REST 重读事实再决定；
    - `heartbeat_required` 标记「已追平且暂无新事件」：服务端应发注释心跳保活。
    """

    if status.is_terminal:
        return ReconnectDecisionV1(
            action=RecoverActionV1.RENDER_RESULT,
            terminal_display=terminal_display_for(status),
            heartbeat_required=False,
        )
    if cursor.sequence > server_last_event_sequence:
        raise SSEStreamError("SSE_CURSOR_AHEAD_OF_SERVER")
    return ReconnectDecisionV1(
        action=RecoverActionV1.STREAM_RESUME,
        resume_from_sequence=cursor.resume_from_sequence,
        heartbeat_required=cursor.sequence >= server_last_event_sequence,
    )


__all__ = [
    "OPEN_STATUSES",
    "SSEStreamError",
    "SSE_HEARTBEAT_COMMENT",
    "SSE_STREAMABLE_EVENT_TYPES",
    "TERMINAL_STATUSES",
    "RecoverActionV1",
    "ReconnectDecisionV1",
    "TaskEventCursorV1",
    "TaskEventProjectionV1",
    "TaskProgressProjectionV1",
    "TaskStatusV1",
    "TerminalDisplayV1",
    "decide_reconnect",
    "is_forbidden_projection_key",
    "parse_event_cursor",
    "project_stream_event",
    "terminal_display_for",
]
