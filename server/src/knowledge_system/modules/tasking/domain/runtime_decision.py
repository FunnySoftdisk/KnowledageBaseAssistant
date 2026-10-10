"""AGT-07 零LLM RuntimeDecisionController 契约与唯一决策权（零LLM、零I/O）。

Controller 只读取不可变 `RuntimeDecisionSnapshotV1`（PG 权威事实的决策信号投影），
按十类指令的固定优先序返回唯一 `RuntimeDecisionV1`。模型、OPA、AGT-06 调度均不产生
运行决策；正文、Prompt 和模型输出不进入 Snapshot 或 Decision。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .core_types import (
    Sha256Hex,
    ShortVersion,
    StrictContractV1,
    VersionedRefV1,
    canonical_json_sha256,
)

RUNTIME_DECISION_NAMESPACE = uuid5(NAMESPACE_URL, "urn:knowledge-system:runtime-decision:v1")

# 冻结的控制器画像版本；动作、优先级、Schema、Digest/Namespace 变化必须升级本契约。
CONTROLLER_PROFILE_VERSION = "runtime-decision-controller-v1"


class RuntimeDirectiveActionV1(StrEnum):
    CLOSE_RUNTIME = "CLOSE_RUNTIME"
    ADVANCE_CANCELLATION = "ADVANCE_CANCELLATION"
    WAIT_RECONCILIATION = "WAIT_RECONCILIATION"
    FAIL_TASK = "FAIL_TASK"
    REQUEST_USER_INPUT = "REQUEST_USER_INPUT"
    START_REPLAN = "START_REPLAN"
    RETRY_PLAN_ITEM = "RETRY_PLAN_ITEM"
    START_FINAL_SYNTHESIS = "START_FINAL_SYNTHESIS"
    SCHEDULE_READY_SET = "SCHEDULE_READY_SET"
    WAIT_EVENT = "WAIT_EVENT"


class TaskRuntimeStateV1(StrEnum):
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RuntimeBlockingIssueKindV1(StrEnum):
    REQUIRED_PARAMETER_MISSING = "REQUIRED_PARAMETER_MISSING"
    RESOURCE_SELECTION_REQUIRED = "RESOURCE_SELECTION_REQUIRED"
    REQUIRED_CONFLICT_DECISION = "REQUIRED_CONFLICT_DECISION"
    USER_OWNED_FACT_REQUIRED = "USER_OWNED_FACT_REQUIRED"


class ReplanAdmissionV1(StrEnum):
    REQUIRED = "REQUIRED"
    ELIGIBLE = "ELIGIBLE"
    BLOCKED = "BLOCKED"
    NOT_NEEDED = "NOT_NEEDED"


class FinalOutcomeKindV1(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class RuntimeDecisionReceiptOutcomeV1(StrEnum):
    APPLIED = "APPLIED"
    WAIT_REGISTERED = "WAIT_REGISTERED"
    STALE = "STALE"
    NO_OP_IDEMPOTENT = "NO_OP_IDEMPOTENT"
    FAILED_CLOSED = "FAILED_CLOSED"


class DecisionReasonV1(StrEnum):
    """十类指令的封闭 Reason 集合；每条指令只接受与之匹配的子集。"""

    TERMINAL_EVENT_COMMITTED = "TERMINAL_EVENT_COMMITTED"
    RUNTIME_CLOSE_READY = "RUNTIME_CLOSE_READY"
    CANCELLATION_PENDING = "CANCELLATION_PENDING"
    GENERATION_FENCE_REQUIRED = "GENERATION_FENCE_REQUIRED"
    DEADLINE_KILL_REQUIRED = "DEADLINE_KILL_REQUIRED"
    BLOCKING_EFFECT_UNRESOLVED = "BLOCKING_EFFECT_UNRESOLVED"
    RECONCILIATION_SCHEDULED = "RECONCILIATION_SCHEDULED"
    INTEGRITY_CORRUPTION = "INTEGRITY_CORRUPTION"
    REQUIRED_STATE_UNRECOVERABLE = "REQUIRED_STATE_UNRECOVERABLE"
    REPLAN_REQUIRED_FAILED = "REPLAN_REQUIRED_FAILED"
    DEADLINE_CANCELLATION_CONVERGED = "DEADLINE_CANCELLATION_CONVERGED"
    CONVERGENCE_BUDGET_CORRUPT = "CONVERGENCE_BUDGET_CORRUPT"
    CONTROL_DEPENDENCY_EXHAUSTED = "CONTROL_DEPENDENCY_EXHAUSTED"
    SAFETY_FAIL_CLOSED = "SAFETY_FAIL_CLOSED"
    REQUIRED_PARAMETER_MISSING = "REQUIRED_PARAMETER_MISSING"
    RESOURCE_SELECTION_REQUIRED = "RESOURCE_SELECTION_REQUIRED"
    REQUIRED_CONFLICT_DECISION = "REQUIRED_CONFLICT_DECISION"
    USER_OWNED_FACT_REQUIRED = "USER_OWNED_FACT_REQUIRED"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    REPLAN_ELIGIBLE = "REPLAN_ELIGIBLE"
    BUSINESS_RETRYABLE_FAILURE = "BUSINESS_RETRYABLE_FAILURE"
    NEW_CAUSAL_FACT = "NEW_CAUSAL_FACT"
    REQUIRED_CRITERION_SATISFIED = "REQUIRED_CRITERION_SATISFIED"
    NO_RUNNABLE_REQUIRED_PATH = "NO_RUNNABLE_REQUIRED_PATH"
    FINAL_PROTECTED_BUDGET = "FINAL_PROTECTED_BUDGET"
    PARTIAL_RESULT_ONLY = "PARTIAL_RESULT_ONLY"
    READY_ITEMS_AVAILABLE = "READY_ITEMS_AVAILABLE"
    WAKE_SOURCE_REGISTERED = "WAKE_SOURCE_REGISTERED"


FAIL_TASK_REASONS: frozenset[DecisionReasonV1] = frozenset(
    {
        DecisionReasonV1.INTEGRITY_CORRUPTION,
        DecisionReasonV1.REQUIRED_STATE_UNRECOVERABLE,
        DecisionReasonV1.REPLAN_REQUIRED_FAILED,
        DecisionReasonV1.DEADLINE_CANCELLATION_CONVERGED,
        DecisionReasonV1.CONVERGENCE_BUDGET_CORRUPT,
        DecisionReasonV1.CONTROL_DEPENDENCY_EXHAUSTED,
        DecisionReasonV1.SAFETY_FAIL_CLOSED,
    }
)

FINALIZATION_REASONS: frozenset[DecisionReasonV1] = frozenset(
    {
        DecisionReasonV1.REQUIRED_CRITERION_SATISFIED,
        DecisionReasonV1.NO_RUNNABLE_REQUIRED_PATH,
        DecisionReasonV1.FINAL_PROTECTED_BUDGET,
        DecisionReasonV1.PARTIAL_RESULT_ONLY,
    }
)


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC_DATETIME_REQUIRED")
    return value.astimezone(UTC)


class RuntimeBlockingIssueV1(StrictContractV1):
    """运行期用户阻断请求的唯一来源；模型不确定、内部选型、R2/R3、预算或调度选择不可触发。"""

    issue_kind: RuntimeBlockingIssueKindV1
    issue_fingerprint: Sha256Hex
    refs: tuple[VersionedRefV1, ...] = Field(max_length=16)


class RetryablePlanItemV1(StrictContractV1):
    """上一 Iteration 已确定终态、下一 Iteration≤3、映射为 business-retryable 且出现新因果事实。"""

    plan_item_id: UUID
    next_iteration_no: int = Field(ge=2, le=3)
    retry_reason: Literal["BUSINESS_RETRYABLE_FAILURE", "NEW_CAUSAL_FACT"]


class WakeSourceV1(StrictContractV1):
    """已登记唤醒源；没有工作/Request/Timer/READY/Retry/Replan/Final 路径时等待即 Deadlock。"""

    kind: Literal[
        "ACTIVE_CHILD",
        "PENDING_APPROVAL",
        "PENDING_USER_INPUT",
        "PENDING_DEFERRED",
        "RUNNING_WAVE",
        "CAPACITY_RELEASE",
        "BUSINESS_BACKOFF",
        "DEPENDENCY_CHANGE",
        "AUTHORIZATION_CHANGE",
        "CAPABILITY_CHANGE",
    ]
    refs: tuple[VersionedRefV1, ...] = Field(max_length=16)
    next_wakeup_at: datetime

    @field_validator("next_wakeup_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return _normalize_utc(value)


class CloseRuntimeDirectiveV1(StrictContractV1):
    action: Literal["CLOSE_RUNTIME"]
    terminal_state: TaskRuntimeStateV1


class AdvanceCancellationDirectiveV1(StrictContractV1):
    action: Literal["ADVANCE_CANCELLATION"]
    terminal_kind: Literal["CANCELLED", "FAILED"] | None = None


class WaitReconciliationDirectiveV1(StrictContractV1):
    action: Literal["WAIT_RECONCILIATION"]
    effect_refs: tuple[VersionedRefV1, ...] = Field(max_length=16)
    next_reconcile_at: datetime

    @field_validator("next_reconcile_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return _normalize_utc(value)


class FailTaskDirectiveV1(StrictContractV1):
    action: Literal["FAIL_TASK"]
    fail_reason: DecisionReasonV1

    @model_validator(mode="after")
    def validate_fail_reason(self) -> FailTaskDirectiveV1:
        if self.fail_reason not in FAIL_TASK_REASONS:
            raise ValueError("RUNTIME_DECISION_FAIL_REASON_INVALID")
        return self


class RequestUserInputDirectiveV1(StrictContractV1):
    action: Literal["REQUEST_USER_INPUT"]
    blocking_issue: RuntimeBlockingIssueV1
    origin: Literal["RUNTIME_RESOLUTION"]


class StartReplanDirectiveV1(StrictContractV1):
    action: Literal["START_REPLAN"]
    admission: Literal["REQUIRED", "ELIGIBLE"]
    base_plan_version: int = Field(ge=1)


class RetryPlanItemDirectiveV1(StrictContractV1):
    action: Literal["RETRY_PLAN_ITEM"]
    retryable_items: tuple[RetryablePlanItemV1, ...] = Field(min_length=1, max_length=16)


class StartFinalSynthesisDirectiveV1(StrictContractV1):
    action: Literal["START_FINAL_SYNTHESIS"]
    outcome_kind: FinalOutcomeKindV1


class ScheduleReadySetDirectiveV1(StrictContractV1):
    action: Literal["SCHEDULE_READY_SET"]
    ready_set_ref: VersionedRefV1


class WaitEventDirectiveV1(StrictContractV1):
    action: Literal["WAIT_EVENT"]
    wake_sources: tuple[WakeSourceV1, ...] = Field(min_length=1, max_length=16)
    next_wakeup_at: datetime
    deadline_at: datetime

    @field_validator("next_wakeup_at", "deadline_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return _normalize_utc(value)


RuntimeDirectiveV1 = Annotated[
    CloseRuntimeDirectiveV1
    | AdvanceCancellationDirectiveV1
    | WaitReconciliationDirectiveV1
    | FailTaskDirectiveV1
    | RequestUserInputDirectiveV1
    | StartReplanDirectiveV1
    | RetryPlanItemDirectiveV1
    | StartFinalSynthesisDirectiveV1
    | ScheduleReadySetDirectiveV1
    | WaitEventDirectiveV1,
    Field(discriminator="action"),
]
RUNTIME_DIRECTIVE_ADAPTER: TypeAdapter[RuntimeDirectiveV1] = TypeAdapter(RuntimeDirectiveV1)


class RuntimeDecisionSnapshotV1(StrictContractV1):
    """PG 权威事实的决策信号投影；正文、Prompt 和模型输出不进入。"""

    contract_version: Literal["runtime_decision_snapshot_v1"]
    task_id: UUID
    task_row_version: int = Field(ge=1)
    event_high_watermark: int = Field(ge=0)
    task_state: TaskRuntimeStateV1
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
    # FAIL_TASK
    fail_reason: DecisionReasonV1 | None = None
    # REQUEST_USER_INPUT
    blocking_issue: RuntimeBlockingIssueV1 | None = None
    # START_REPLAN
    replan_admission: ReplanAdmissionV1
    replan_budget_complete: bool
    has_active_replan: bool
    snapshot_version_stable: bool
    has_unaffected_ready_items: bool
    # RETRY_PLAN_ITEM
    retryable_items: tuple[RetryablePlanItemV1, ...] = Field(max_length=16)
    # START_FINAL_SYNTHESIS
    final_outcome_kind: FinalOutcomeKindV1 | None = None
    finalization_reason: DecisionReasonV1 | None = None
    contributions_merged: bool
    # SCHEDULE_READY_SET
    has_ready_items: bool
    ready_set_ref: VersionedRefV1 | None = None
    scheduling_state_valid: bool
    # WAIT_EVENT
    wake_sources: tuple[WakeSourceV1, ...] = Field(max_length=16)
    snapshot_digest: Sha256Hex = "0" * 64

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
    def validate_signals(self) -> RuntimeDecisionSnapshotV1:
        if self.fail_reason is not None and self.fail_reason not in FAIL_TASK_REASONS:
            raise ValueError("RUNTIME_DECISION_FAIL_REASON_INVALID")
        if (self.final_outcome_kind is None) != (self.finalization_reason is None):
            raise ValueError("RUNTIME_DECISION_FINALIZATION_SIGNAL_MISMATCH")
        if (
            self.finalization_reason is not None
            and self.finalization_reason not in FINALIZATION_REASONS
        ):
            raise ValueError("RUNTIME_DECISION_FINALIZATION_REASON_INVALID")
        if self.has_ready_items and self.ready_set_ref is None:
            raise ValueError("RUNTIME_DECISION_READY_SET_REF_REQUIRED")
        if (
            self.has_blocking_unknown_effect
            and self.reconciliation_ready
            and (self.next_reconcile_at is None)
        ):
            raise ValueError("RUNTIME_DECISION_RECONCILE_AT_REQUIRED")
        return self


def snapshot_digest_for(snapshot: RuntimeDecisionSnapshotV1) -> str:
    return canonical_json_sha256(snapshot.model_dump(mode="json", exclude={"snapshot_digest"}))


def freeze_runtime_decision_snapshot(
    snapshot: RuntimeDecisionSnapshotV1,
) -> RuntimeDecisionSnapshotV1:
    """以 RFC 8785 JCS+SHA-256（排除 snapshot_digest 自身）冻结不可变快照。"""

    return snapshot.model_copy(update={"snapshot_digest": snapshot_digest_for(snapshot)})


def decision_id(task_id: UUID, snapshot_digest: Sha256Hex) -> UUID:
    """同一依据（task_id:snapshot_digest:profile_version）必须派生同一决策 ID。"""

    name = f"{str(task_id).lower()}:{snapshot_digest}:{CONTROLLER_PROFILE_VERSION}"
    return uuid5(RUNTIME_DECISION_NAMESPACE, name)


class RuntimeDecisionV1(StrictContractV1):
    contract_version: Literal["runtime_decision_v1"]
    decision_id: UUID
    decision_sequence: int = Field(ge=1)
    snapshot_digest: Sha256Hex
    controller_profile_version: ShortVersion
    directive: RuntimeDirectiveV1
    reasons: tuple[DecisionReasonV1, ...] = Field(min_length=1, max_length=8)
    basis_task_row_version: int = Field(ge=1)
    basis_event_high_watermark: int = Field(ge=0)
    issued_task_row_version: int = Field(ge=1)
    input_id: UUID
    input_version: int = Field(ge=1)
    plan_version: int = Field(ge=1)
    work_version: int = Field(ge=1)
    execution_generation: int = Field(ge=0)
    cancellation_generation: int = Field(ge=0)
    decision_digest: Sha256Hex

    @model_validator(mode="after")
    def validate_reasons(self) -> RuntimeDecisionV1:
        action = RuntimeDirectiveActionV1(self.directive.action)
        allowed = _REASONS_FOR_ACTION[action]
        if not all(reason in allowed for reason in self.reasons):
            raise ValueError("RUNTIME_DECISION_REASON_MISMATCH")
        return self


class RuntimeDecisionRefV1(StrictContractV1):
    """Activity 只返回此小引用；Root 以穷尽式 match 分发，未知动作失败关闭。"""

    decision_id: UUID
    decision_sequence: int = Field(ge=1)
    action: RuntimeDirectiveActionV1
    decision_digest: Sha256Hex
    expected_task_row_version: int = Field(ge=1)


class RuntimeDecisionReceiptV1(StrictContractV1):
    """一个 Decision 只有一个终态 Receipt；重复应用返回既有结果引用。"""

    decision_id: UUID
    outcome: RuntimeDecisionReceiptOutcomeV1
    decision_sequence: int = Field(ge=1)
    receipt_digest: Sha256Hex


_REASONS_FOR_ACTION: dict[RuntimeDirectiveActionV1, frozenset[DecisionReasonV1]] = {
    RuntimeDirectiveActionV1.CLOSE_RUNTIME: frozenset(
        {DecisionReasonV1.TERMINAL_EVENT_COMMITTED, DecisionReasonV1.RUNTIME_CLOSE_READY}
    ),
    RuntimeDirectiveActionV1.ADVANCE_CANCELLATION: frozenset(
        {
            DecisionReasonV1.CANCELLATION_PENDING,
            DecisionReasonV1.GENERATION_FENCE_REQUIRED,
            DecisionReasonV1.DEADLINE_KILL_REQUIRED,
        }
    ),
    RuntimeDirectiveActionV1.WAIT_RECONCILIATION: frozenset(
        {
            DecisionReasonV1.BLOCKING_EFFECT_UNRESOLVED,
            DecisionReasonV1.RECONCILIATION_SCHEDULED,
        }
    ),
    RuntimeDirectiveActionV1.FAIL_TASK: FAIL_TASK_REASONS,
    RuntimeDirectiveActionV1.REQUEST_USER_INPUT: frozenset(
        {
            DecisionReasonV1.REQUIRED_PARAMETER_MISSING,
            DecisionReasonV1.RESOURCE_SELECTION_REQUIRED,
            DecisionReasonV1.REQUIRED_CONFLICT_DECISION,
            DecisionReasonV1.USER_OWNED_FACT_REQUIRED,
        }
    ),
    RuntimeDirectiveActionV1.START_REPLAN: frozenset(
        {DecisionReasonV1.REPLAN_REQUIRED, DecisionReasonV1.REPLAN_ELIGIBLE}
    ),
    RuntimeDirectiveActionV1.RETRY_PLAN_ITEM: frozenset(
        {DecisionReasonV1.BUSINESS_RETRYABLE_FAILURE, DecisionReasonV1.NEW_CAUSAL_FACT}
    ),
    RuntimeDirectiveActionV1.START_FINAL_SYNTHESIS: FINALIZATION_REASONS,
    RuntimeDirectiveActionV1.SCHEDULE_READY_SET: frozenset(
        {DecisionReasonV1.READY_ITEMS_AVAILABLE}
    ),
    RuntimeDirectiveActionV1.WAIT_EVENT: frozenset({DecisionReasonV1.WAKE_SOURCE_REGISTERED}),
}


def _ordered_reasons(
    primary: DecisionReasonV1, extra: tuple[DecisionReasonV1, ...] = ()
) -> tuple[DecisionReasonV1, ...]:
    reasons: list[DecisionReasonV1] = [primary]
    for reason in extra:
        if reason not in reasons:
            reasons.append(reason)
    return tuple(reasons)


def _select_directive(
    snapshot: RuntimeDecisionSnapshotV1,
) -> tuple[RuntimeDirectiveV1, tuple[DecisionReasonV1, ...]]:
    directive: RuntimeDirectiveV1
    terminal = {
        TaskRuntimeStateV1.COMPLETED,
        TaskRuntimeStateV1.FAILED,
        TaskRuntimeStateV1.CANCELLED,
    }
    if (
        snapshot.task_state in terminal
        and snapshot.terminal_committed
        and not snapshot.has_active_execution_unit
        and not snapshot.has_unresolved_unknown_effect
        and snapshot.runtime_close_ready
    ):
        directive = CloseRuntimeDirectiveV1(
            action="CLOSE_RUNTIME", terminal_state=snapshot.task_state
        )
        reasons = _ordered_reasons(
            DecisionReasonV1.TERMINAL_EVENT_COMMITTED, (DecisionReasonV1.RUNTIME_CLOSE_READY,)
        )
        return directive, reasons

    if snapshot.has_pending_cancellation and (
        snapshot.task_state == TaskRuntimeStateV1.CANCEL_REQUESTED
        or snapshot.cancellation_fence_required
    ):
        extra: tuple[DecisionReasonV1, ...] = (
            (DecisionReasonV1.GENERATION_FENCE_REQUIRED,)
            if snapshot.cancellation_fence_required
            else ()
        )
        directive = AdvanceCancellationDirectiveV1(
            action="ADVANCE_CANCELLATION", terminal_kind=snapshot.cancellation_terminal_kind
        )
        return directive, _ordered_reasons(DecisionReasonV1.CANCELLATION_PENDING, extra)

    if (
        snapshot.has_blocking_unknown_effect
        and snapshot.reconciliation_ready
        and snapshot.next_reconcile_at is not None
    ):
        directive = WaitReconciliationDirectiveV1(
            action="WAIT_RECONCILIATION",
            effect_refs=snapshot.blocking_effect_refs,
            next_reconcile_at=snapshot.next_reconcile_at,
        )
        reasons = _ordered_reasons(
            DecisionReasonV1.BLOCKING_EFFECT_UNRESOLVED,
            (DecisionReasonV1.RECONCILIATION_SCHEDULED,),
        )
        return directive, reasons

    if snapshot.fail_reason is not None:
        directive = FailTaskDirectiveV1(action="FAIL_TASK", fail_reason=snapshot.fail_reason)
        return directive, (snapshot.fail_reason,)

    if snapshot.blocking_issue is not None:
        directive = RequestUserInputDirectiveV1(
            action="REQUEST_USER_INPUT",
            blocking_issue=snapshot.blocking_issue,
            origin="RUNTIME_RESOLUTION",
        )
        return directive, (DecisionReasonV1(snapshot.blocking_issue.issue_kind.value),)

    if (
        snapshot.replan_admission in (ReplanAdmissionV1.REQUIRED, ReplanAdmissionV1.ELIGIBLE)
        and not snapshot.has_active_replan
        and snapshot.replan_budget_complete
        and snapshot.snapshot_version_stable
        and not snapshot.has_blocking_unknown_effect
        and not (
            snapshot.replan_admission == ReplanAdmissionV1.ELIGIBLE
            and snapshot.has_unaffected_ready_items
        )
    ):
        directive = StartReplanDirectiveV1(
            action="START_REPLAN",
            admission=snapshot.replan_admission.value,  # type: ignore[arg-type]
            base_plan_version=snapshot.plan_version,
        )
        reason = (
            DecisionReasonV1.REPLAN_REQUIRED
            if snapshot.replan_admission == ReplanAdmissionV1.REQUIRED
            else DecisionReasonV1.REPLAN_ELIGIBLE
        )
        return directive, (reason,)

    if snapshot.retryable_items and not snapshot.has_blocking_unknown_effect:
        directive = RetryPlanItemDirectiveV1(
            action="RETRY_PLAN_ITEM", retryable_items=snapshot.retryable_items
        )
        reasons = tuple(DecisionReasonV1(item.retry_reason) for item in snapshot.retryable_items)
        return directive, tuple(dict.fromkeys(reasons))

    if (
        snapshot.final_outcome_kind is not None
        and not snapshot.has_active_execution_unit
        and snapshot.contributions_merged
        and snapshot.snapshot_version_stable
        and not snapshot.has_blocking_unknown_effect
        and snapshot.blocking_issue is None
    ):
        directive = StartFinalSynthesisDirectiveV1(
            action="START_FINAL_SYNTHESIS", outcome_kind=snapshot.final_outcome_kind
        )
        assert snapshot.finalization_reason is not None
        return directive, (snapshot.finalization_reason,)

    if (
        snapshot.task_state == TaskRuntimeStateV1.RUNNING
        and snapshot.has_ready_items
        and snapshot.scheduling_state_valid
        and not snapshot.has_blocking_unknown_effect
    ):
        assert snapshot.ready_set_ref is not None
        directive = ScheduleReadySetDirectiveV1(
            action="SCHEDULE_READY_SET", ready_set_ref=snapshot.ready_set_ref
        )
        return directive, (DecisionReasonV1.READY_ITEMS_AVAILABLE,)

    if snapshot.wake_sources:
        next_wakeup_at = min(source.next_wakeup_at for source in snapshot.wake_sources)
        directive = WaitEventDirectiveV1(
            action="WAIT_EVENT",
            wake_sources=snapshot.wake_sources,
            next_wakeup_at=next_wakeup_at,
            deadline_at=snapshot.deadline_at,
        )
        return directive, (DecisionReasonV1.WAKE_SOURCE_REGISTERED,)

    raise ValueError("RUNTIME_DEADLOCK_DETECTED")


def evaluate_runtime_decision(
    snapshot: RuntimeDecisionSnapshotV1, decision_sequence: int
) -> RuntimeDecisionV1:
    """冻结快照 → 按固定优先序求唯一决策 → 冻结决策（同依据同 ID/同 Digest）。"""

    frozen = freeze_runtime_decision_snapshot(snapshot)
    directive, reasons = _select_directive(frozen)
    decision_id_value = decision_id(frozen.task_id, frozen.snapshot_digest)
    decision = RuntimeDecisionV1(
        contract_version="runtime_decision_v1",
        decision_id=decision_id_value,
        decision_sequence=decision_sequence,
        snapshot_digest=frozen.snapshot_digest,
        controller_profile_version=CONTROLLER_PROFILE_VERSION,
        directive=directive,
        reasons=reasons,
        basis_task_row_version=frozen.task_row_version,
        basis_event_high_watermark=frozen.event_high_watermark,
        issued_task_row_version=frozen.task_row_version,
        input_id=frozen.input_id,
        input_version=frozen.input_version,
        plan_version=frozen.plan_version,
        work_version=frozen.work_version,
        execution_generation=frozen.execution_generation,
        cancellation_generation=frozen.cancellation_generation,
        decision_digest="0" * 64,
    )
    digest = canonical_json_sha256(decision.model_dump(mode="json", exclude={"decision_digest"}))
    return decision.model_copy(update={"decision_digest": digest})
