"""DEV-06 运行时决策的事实契约（零 LLM、零 I/O）。

`RuntimeDecisionFactsV1` 只承载**低级原始事实**（身份/版本、DB 状态、生命周期布尔、引用、
上一片切已产出的 `RuntimeConvergenceActionV1` + `NoProgressStateV1` 收敛事实）。任何派生信号
（`replan_admission` / `replan_budget_complete` / `finalization_reason` / `fail_reason` /
`final_outcome_kind` / `contributions_merged` 覆盖）都由 `runtime_decision_builder`
产出，不在此契约。

DB 状态串到领域 `TaskRuntimeStateV1` 的冻结映射由 `map_task_status` 提供，未知名 fail-closed，
防止上游持久化层的新状态串静默穿过决策投影。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from knowledge_system.modules.tasking.application.runtime_convergence import (
    RuntimeConvergenceActionV1,
)
from knowledge_system.modules.tasking.domain.core_types import StrictContractV1, VersionedRefV1
from knowledge_system.modules.tasking.domain.runtime_decision import (
    RetryablePlanItemV1,
    RuntimeBlockingIssueV1,
    TaskRuntimeStateV1,
    WakeSourceV1,
    _normalize_utc,
)
from knowledge_system.modules.tasking.domain.runtime_progress import (
    MAX_REPLAN_PER_TASK,
    NoProgressStateV1,
)

# 上游持久化层允许的任务状态串（冻结白名单）。活跃态归并为 RUNNING，等待态由
# `blocking_issue` / `wake_sources` 表达，不独占运行时状态。
DB_TASK_STATUSES: frozenset[str] = frozenset(
    {
        "QUEUED",
        "PLANNING",
        "RUNNING",
        "REPLANNING",
        "WAITING_USER_INPUT",
        "WAITING_APPROVAL",
        "RETRY_WAIT",
        "FINALIZING",
        "CANCEL_REQUESTED",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
    }
)

_ACTIVE_DB_STATUSES: frozenset[str] = frozenset(
    {
        "QUEUED",
        "PLANNING",
        "RUNNING",
        "REPLANNING",
        "WAITING_USER_INPUT",
        "WAITING_APPROVAL",
        "RETRY_WAIT",
    }
)

_TERMINALISH_DB_STATUSES: frozenset[str] = frozenset(
    {"FINALIZING", "CANCEL_REQUESTED", "COMPLETED", "FAILED", "CANCELLED"}
)


def map_task_status(db_status: str) -> TaskRuntimeStateV1:
    """DB 状态串 → 领域运行时状态；未知名 fail-closed，不静默投影。"""

    if db_status in _ACTIVE_DB_STATUSES:
        return TaskRuntimeStateV1.RUNNING
    if db_status in _TERMINALISH_DB_STATUSES:
        return TaskRuntimeStateV1(db_status)
    raise ValueError("RUNTIME_DECISION_DB_STATUS_UNKNOWN")


class RuntimeDecisionFactsV1(StrictContractV1):
    """决策快照投影所需的低级原始事实；不含任何派生信号。"""

    task_id: UUID
    task_row_version: int = Field(ge=1)
    event_high_watermark: int = Field(ge=0)
    db_status: str
    input_id: UUID
    input_version: int = Field(ge=1)
    plan_version: int = Field(ge=1)
    work_version: int = Field(ge=1)
    execution_generation: int = Field(ge=0)
    cancellation_generation: int = Field(ge=0)
    deadline_at: datetime
    # CLOSE_RUNTIME
    terminal_committed: bool
    runtime_close_ready: bool
    has_active_execution_unit: bool
    has_unresolved_unknown_effect: bool
    # ADVANCE_CANCELLATION
    has_pending_cancellation: bool
    cancellation_fence_required: bool
    cancellation_terminal_kind: Literal["CANCELLED", "FAILED"] | None = None
    # WAIT_RECONCILIATION
    has_blocking_unknown_effect: bool
    reconciliation_ready: bool
    blocking_effect_refs: tuple[VersionedRefV1, ...] = Field(max_length=16)
    next_reconcile_at: datetime | None = None
    # REQUEST_USER_INPUT
    blocking_issue: RuntimeBlockingIssueV1 | None = None
    # START_REPLAN
    has_active_replan: bool
    snapshot_version_stable: bool
    has_unaffected_ready_items: bool
    # RETRY_PLAN_ITEM
    retryable_items: tuple[RetryablePlanItemV1, ...] = Field(max_length=16)
    # START_FINAL_SYNTHESIS
    contributions_merged: bool
    # SCHEDULE_READY_SET
    has_ready_items: bool
    ready_set_ref: VersionedRefV1 | None = None
    scheduling_state_valid: bool
    # WAIT_EVENT
    wake_sources: tuple[WakeSourceV1, ...] = Field(max_length=16)
    # 收敛/重规划原始事实（上一片切 `merge_loop_no_progress` 产出，或后续 PG 只读仓库投影）
    replan_required: bool = False
    convergence_action: RuntimeConvergenceActionV1
    no_progress_state: NoProgressStateV1
    has_partial_results: bool = False
    required_criteria_satisfied: bool = False

    @field_validator("db_status")
    @classmethod
    def validate_db_status(cls, value: str) -> str:
        if value not in DB_TASK_STATUSES:
            raise ValueError("RUNTIME_DECISION_DB_STATUS_UNKNOWN")
        return value

    @field_validator("deadline_at")
    @classmethod
    def normalize_deadline(cls, value: datetime) -> datetime:
        return _normalize_utc(value)

    @field_validator("next_reconcile_at")
    @classmethod
    def normalize_reconcile(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _normalize_utc(value)

    @model_validator(mode="after")
    def validate_replan_coherence(self) -> RuntimeDecisionFactsV1:
        # REPLAN 动作必须携带已消费的预算状态（`mark_replan_used` 不变式）。
        if self.convergence_action is RuntimeConvergenceActionV1.REPLAN:
            if not self.no_progress_state.episode_replan_used:
                raise ValueError("RUNTIME_DECISION_REPLAN_EPISODE_NOT_MARKED")
            if not 1 <= self.no_progress_state.task_replan_count <= MAX_REPLAN_PER_TASK:
                raise ValueError("RUNTIME_DECISION_REPLAN_BUDGET_OUT_OF_RANGE")
        return self


__all__ = ["DB_TASK_STATUSES", "RuntimeDecisionFactsV1", "map_task_status"]
