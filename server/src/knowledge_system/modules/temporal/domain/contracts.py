"""DEV-03A：Temporal可靠执行骨架的冻结契约（严格Pydantic判别联合）。

只承载Root/Child Workflow的输入输出与运行态引用，不含正文、Run ID、Client、Secret或
active Child Handle。所有模型`extra=forbid`、`frozen=True`；首Run序号为1，续跑输入序号为
`current+1`。Root/Child输入输出经`temporalio.contrib.pydantic`以JSON往返，因此`datetime`
字段以ISO字符串反序列化后再由校验器规范为UTC。输入输出只含已提交引用、Digest与用量；
原始文件/全文/二进制/Tool原包及大载荷只由Artifact持久化；小型Replay必需载荷待
DEV-03D Payload Codec落地后可以加密形式进入History，Secret永不进入。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import ConfigDict, Field, RootModel, field_validator, model_validator

from knowledge_system.modules.tasking.domain.core_types import (
    ArtifactRefV1,
    Sha256Hex,
    StrictContractV1,
)


class TemporalContractV1(StrictContractV1):
    """Temporal契约基类：与任务域共享严格约束，但关闭`strict`以兼容JSON往返。

    任务域`StrictContractV1`用`strict=True`；Temporal契约经JSON序列化往返，`datetime`
    会以ISO字符串重新进入，因此本基类关闭strict并用手写校验器兜底UTC与时区。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class AttemptKindV1(StrEnum):
    """Child `AgentAttemptWorkflow`的五类Attempt Spec。"""

    GOAL_UNDERSTANDING = "GOAL_UNDERSTANDING"
    PLAN = "PLAN"
    REPLAN = "REPLAN"
    ACTION = "ACTION"
    FINAL_SYNTHESIS = "FINAL_SYNTHESIS"


class AgentTaskRunStateV1(StrEnum):
    """Root粗粒度运行态（低敏，可入Search Attribute）。"""

    STARTED = "STARTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AgentRegistrationKeyV1(StrEnum):
    """固定Agent注册键。Root固定`supervisor-v1`；其余内置键与临时键随DEV-05注册。"""

    SUPERVISOR = "supervisor-v1"


class TaskRefV1(TemporalContractV1):
    """Task身份引用；正文与Input仍由PG/Artifact持久化。"""

    task_id: UUID


class TaskEventRefV1(TemporalContractV1):
    """`TASK_CREATED`事件引用。"""

    event_id: UUID
    sequence: int = Field(ge=1)


class RuntimeContractRefV1(TemporalContractV1):
    """Runtime契约版本引用；不含实现细节。"""

    version: Literal["agent_runtime_v1"] = "agent_runtime_v1"


class TaskBudgetV1(TemporalContractV1):
    """冻结Task预算。`wall_clock_deadline`是唯一业务总截止时间。

    预算明细字段（模型调用次数、能力配额、Stop Condition等）随DEV-05冻结；本批只承载
    可靠执行骨架所需的业务总截止时间。等待审批/用户输入/重试与系统中断均计入截止。
    """

    wall_clock_deadline: datetime

    @field_validator("wall_clock_deadline")
    @classmethod
    def _normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("WALL_CLOCK_DEADLINE_UTC_REQUIRED")
        return value.astimezone(UTC)


class AgentTaskWorkflowStartV1(TemporalContractV1):
    """Root首Run输入（kind=START）。"""

    kind: Literal["START"] = "START"
    task: TaskRefV1
    task_created_event: TaskEventRefV1
    runtime_contract: RuntimeContractRefV1
    budget: TaskBudgetV1
    run_sequence: int = Field(ge=1)

    @model_validator(mode="after")
    def _first_run_sequence(self) -> AgentTaskWorkflowStartV1:
        if self.run_sequence != 1:
            raise ValueError("START_RUN_SEQUENCE_MUST_BE_ONE")
        return self


class PendingBusinessRequestRefV1(TemporalContractV1):
    """未决业务请求（待审批/待用户输入）的稳定引用；细节随DEV-03C冻结。"""

    request_id: UUID
    request_kind: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")


class ContextVersionRefV1(TemporalContractV1):
    """Context版本引用（不含正文）。"""

    context_kind: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    version: int = Field(ge=1)
    digest: Sha256Hex


class BudgetUsageEntryV1(TemporalContractV1):
    """Continue-As-New携带的一项预算实耗；计数器名由后续预算契约登记。"""

    counter: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    used: int = Field(ge=0)


class AgentTaskRootStateV1(TemporalContractV1):
    """Root在Continue-As-New边界传递的运行态快照。"""

    event_cursor: int = Field(ge=0)
    business_cursor: int = Field(ge=0)
    run_state: AgentTaskRunStateV1
    plan_version: int | None = Field(default=None, ge=1)
    attempt_kind: AttemptKindV1 | None = None
    agent_registration_key: AgentRegistrationKeyV1 | None = None
    needs_reconcile: bool = False


class AgentTaskWorkflowContinueV1(TemporalContractV1):
    """Root续跑输入（kind=CONTINUE）；序号为`current+1`，PG版本只作不倒退下界。"""

    kind: Literal["CONTINUE"] = "CONTINUE"
    task: TaskRefV1
    root_state: AgentTaskRootStateV1
    budget: TaskBudgetV1
    budget_used: tuple[BudgetUsageEntryV1, ...] = Field(default_factory=tuple)
    pending_requests: tuple[PendingBusinessRequestRefV1, ...] = Field(default_factory=tuple)
    context_versions: tuple[ContextVersionRefV1, ...] = Field(default_factory=tuple)
    run_sequence: int = Field(ge=1)

    @model_validator(mode="after")
    def _equal_cursors(self) -> AgentTaskWorkflowContinueV1:
        if self.root_state.event_cursor != self.root_state.business_cursor:
            raise ValueError("CONTINUE_DUAL_CURSORS_NOT_EQUAL")
        if tuple(item.counter for item in self.budget_used) != tuple(
            sorted(item.counter for item in self.budget_used)
        ) or len({item.counter for item in self.budget_used}) != len(self.budget_used):
            raise ValueError("CONTINUE_BUDGET_USED_NOT_UNIQUE_SORTED")
        pending_keys = tuple(
            (item.request_kind, str(item.request_id)) for item in self.pending_requests
        )
        if pending_keys != tuple(sorted(pending_keys)) or len(set(pending_keys)) != len(
            pending_keys
        ):
            raise ValueError("CONTINUE_PENDING_REQUESTS_NOT_UNIQUE_SORTED")
        context_keys = tuple(
            (item.context_kind, item.version, item.digest) for item in self.context_versions
        )
        if context_keys != tuple(sorted(context_keys)) or len(set(context_keys)) != len(
            context_keys
        ):
            raise ValueError("CONTINUE_CONTEXT_VERSIONS_NOT_UNIQUE_SORTED")
        return self


AgentTaskWorkflowArgsV1 = RootModel[
    Annotated[
        Union[AgentTaskWorkflowStartV1, AgentTaskWorkflowContinueV1],  # noqa: UP007
        Field(discriminator="kind"),
    ]
]


class AgentTaskRuntimeStateV1(TemporalContractV1):
    """`runtime_state_v1`同步只读Query的返回视图。"""

    task_id: UUID
    run_sequence: int = Field(ge=1)
    run_state: AgentTaskRunStateV1
    event_cursor: int = Field(ge=0)
    business_cursor: int = Field(ge=0)
    attempt_kind: AttemptKindV1 | None = None
    agent_registration_key: AgentRegistrationKeyV1 | None = None
    deadline: datetime
    needs_reconcile: bool = False

    @field_validator("deadline")
    @classmethod
    def _normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("DEADLINE_UTC_REQUIRED")
        return value.astimezone(UTC)


class AgentTaskWorkflowResultV1(TemporalContractV1):
    """Root终态返回。"""

    task_id: UUID
    run_sequence: int = Field(ge=1)
    terminal_state: AgentTaskRunStateV1
    error_code: str | None = None

    @model_validator(mode="after")
    def _terminal_only(self) -> AgentTaskWorkflowResultV1:
        if self.terminal_state not in (
            AgentTaskRunStateV1.COMPLETED,
            AgentTaskRunStateV1.FAILED,
            AgentTaskRunStateV1.CANCELLED,
        ):
            raise ValueError("WORKFLOW_RESULT_NOT_TERMINAL")
        return self


class AgentAttemptWorkflowInputV1(TemporalContractV1):
    """Child `AgentAttemptWorkflow`输入；Attempt Spec细节随DEV-03C冻结。"""

    task_id: UUID
    task_attempt_id: UUID
    attempt_kind: AttemptKindV1
    plan_version: int | None = Field(default=None, ge=1)
    run_sequence: int = Field(ge=1)
    parent_run_sequence: int = Field(ge=1)


class AttemptCompletedV1(TemporalContractV1):
    """Child正常完成；只携带已提交结果引用。"""

    kind: Literal["COMPLETED"] = "COMPLETED"
    task_attempt_id: UUID
    result_ref: ArtifactRefV1 | None = None


class AttemptSuspendedV1(TemporalContractV1):
    """Child因Deferred Tool/审批而挂起；携带Resume Bundle引用。"""

    kind: Literal["SUSPENDED"] = "SUSPENDED"
    task_attempt_id: UUID
    resume_bundle_ref: ArtifactRefV1 | None = None


class AttemptFailedV1(TemporalContractV1):
    """Child失败；取消保持Temporal CANCELLED，解码/非确定性/代码Bug保持FAILED。"""

    kind: Literal["FAILED"] = "FAILED"
    task_attempt_id: UUID
    error_code: str
    reason: str | None = None


AgentAttemptResultV1 = RootModel[
    Annotated[
        Union[AttemptCompletedV1, AttemptSuspendedV1, AttemptFailedV1],  # noqa: UP007
        Field(discriminator="kind"),
    ]
]


class ApplyBusinessEventsCommandV1(TemporalContractV1):
    """Root control Activity：按游标推进业务事件的命令。"""

    task_id: UUID
    from_sequence: int = Field(ge=0)


class ApplyBusinessEventsResultV1(TemporalContractV1):
    """按游标推进后的结果；`terminal_requested`表示读到终态事件。"""

    task_id: UUID
    applied_count: int = Field(ge=0)
    new_cursor: int = Field(ge=0)
    terminal_requested: Literal["FAILED", "CANCELLED"] | None = None


class RootTerminalCommitV1(TemporalContractV1):
    """Root control Activity：终态CAS提交命令。"""

    task_id: UUID
    run_sequence: int = Field(ge=1)
    terminal_state: AgentTaskRunStateV1
    error_code: str | None = None

    @model_validator(mode="after")
    def _fail_or_cancel_only(self) -> RootTerminalCommitV1:
        if self.terminal_state not in (
            AgentTaskRunStateV1.FAILED,
            AgentTaskRunStateV1.CANCELLED,
        ):
            raise ValueError("ROOT_TERMINAL_COMMIT_NOT_FAIL_OR_CANCEL")
        return self


__all__ = [
    "AgentAttemptResultV1",
    "AgentAttemptWorkflowInputV1",
    "AgentRegistrationKeyV1",
    "AgentTaskRootStateV1",
    "AgentTaskRunStateV1",
    "AgentTaskRuntimeStateV1",
    "AgentTaskWorkflowArgsV1",
    "AgentTaskWorkflowContinueV1",
    "AgentTaskWorkflowResultV1",
    "AgentTaskWorkflowStartV1",
    "ApplyBusinessEventsCommandV1",
    "ApplyBusinessEventsResultV1",
    "AttemptCompletedV1",
    "AttemptFailedV1",
    "AttemptKindV1",
    "AttemptSuspendedV1",
    "BudgetUsageEntryV1",
    "ContextVersionRefV1",
    "PendingBusinessRequestRefV1",
    "RootTerminalCommitV1",
    "RuntimeContractRefV1",
    "TaskBudgetV1",
    "TaskEventRefV1",
    "TaskRefV1",
    "TemporalContractV1",
]
