"""AGT-08 §5.35：Finalization 子状态机、失败/取消/恢复的确定性规则与错误码。

本模块落地 §5.35 的纯领域规则：Final 子状态固定 `CREATED -> SNAPSHOT_BOUND -> MODEL_RUNNING ->
CANDIDATE_STAGED -> VALIDATING -> REPAIRING -> ACCEPTED -> ASSEMBLING -> COMMITTING -> COMMITTED`
且只前进，终止为 `INVALIDATED_STALE/FAILED/CANCELLED`；十种终态错误码与两种非终态控制结果；
连续 Snapshot 重建 ≤3 次（第 4 次 `FINAL_SYNTHESIS_INPUT_UNSTABLE`）、Deadline 超期判定、模型
调用恢复分类（复用/同一 Binding 重试/先对账）与取消对提交的线性化。

Temporal Activity Attempt 上限（`MODEL_REQUEST`≤2、`CONTROL_TRANSACTION`≤5）在此以常量固化，
实际 Activity 重试、PG 恢复重放、锁序与提交事务属于编排/持久层切片，本切片只固化判定输入与
终态映射，不接真实模型、不落地新 ORM/迁移。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal


class FinalizationSubstateV1(StrEnum):
    CREATED = "CREATED"
    SNAPSHOT_BOUND = "SNAPSHOT_BOUND"
    MODEL_RUNNING = "MODEL_RUNNING"
    CANDIDATE_STAGED = "CANDIDATE_STAGED"
    VALIDATING = "VALIDATING"
    REPAIRING = "REPAIRING"
    ACCEPTED = "ACCEPTED"
    ASSEMBLING = "ASSEMBLING"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    INVALIDATED_STALE = "INVALIDATED_STALE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class FinalErrorCodeV1(StrEnum):
    FINAL_MODEL_PROVIDER_UNAVAILABLE = "FINAL_MODEL_PROVIDER_UNAVAILABLE"
    FINAL_MODEL_OUTPUT_INCOMPLETE = "FINAL_MODEL_OUTPUT_INCOMPLETE"
    FINAL_MODEL_OUTPUT_INVALID = "FINAL_MODEL_OUTPUT_INVALID"
    FINAL_MODEL_OUTPUT_UNSAFE = "FINAL_MODEL_OUTPUT_UNSAFE"
    FINAL_CANDIDATE_UNRECOVERABLE = "FINAL_CANDIDATE_UNRECOVERABLE"
    FINAL_RESULT_RENDER_FAILED = "FINAL_RESULT_RENDER_FAILED"
    TASK_RESULT_COMMIT_FAILED = "TASK_RESULT_COMMIT_FAILED"
    TASK_RESULT_COMMIT_CONFLICT = "TASK_RESULT_COMMIT_CONFLICT"
    TASK_RESULT_CONTRACT_MISMATCH = "TASK_RESULT_CONTRACT_MISMATCH"
    TASK_DEADLINE_EXCEEDED = "TASK_DEADLINE_EXCEEDED"
    FINAL_SYNTHESIS_SNAPSHOT_STALE = "FINAL_SYNTHESIS_SNAPSHOT_STALE"
    FINAL_CANDIDATE_REPAIR_REQUIRED = "FINAL_CANDIDATE_REPAIR_REQUIRED"


class FinalModelCallStateV1(StrEnum):
    COMPLETED = "COMPLETED"
    TRANSIENT_NO_DELTA = "TRANSIENT_NO_DELTA"
    STREAMING_INTERRUPTED = "STREAMING_INTERRUPTED"
    UNKNOWN = "UNKNOWN"


class FinalModelCallResumptionV1(StrEnum):
    REUSE_COMPLETED = "REUSE_COMPLETED"
    RETRY_SAME_BINDING = "RETRY_SAME_BINDING"
    RECONCILE_FIRST = "RECONCILE_FIRST"


DeadlineVerdictV1 = Literal["WITHIN_DEADLINE", "TASK_DEADLINE_EXCEEDED"]
SnapshotRebuildVerdictV1 = Literal["ALLOWED", "FINAL_SYNTHESIS_INPUT_UNSTABLE"]
CancelCommitWinnerV1 = Literal["CANCEL", "COMMIT"]

FINAL_MODEL_REQUEST_ATTEMPTS = 2
FINAL_CONTROL_TRANSACTION_ATTEMPTS = 5
FINAL_SNAPSHOT_REBUILD_LIMIT = 3

FINAL_TERMINAL_SUBSTATES: frozenset[FinalizationSubstateV1] = frozenset(
    {
        FinalizationSubstateV1.INVALIDATED_STALE,
        FinalizationSubstateV1.FAILED,
        FinalizationSubstateV1.CANCELLED,
    }
)

FINAL_TERMINAL_ERROR_CODES: frozenset[FinalErrorCodeV1] = frozenset(
    {
        FinalErrorCodeV1.FINAL_MODEL_PROVIDER_UNAVAILABLE,
        FinalErrorCodeV1.FINAL_MODEL_OUTPUT_INCOMPLETE,
        FinalErrorCodeV1.FINAL_MODEL_OUTPUT_INVALID,
        FinalErrorCodeV1.FINAL_MODEL_OUTPUT_UNSAFE,
        FinalErrorCodeV1.FINAL_CANDIDATE_UNRECOVERABLE,
        FinalErrorCodeV1.FINAL_RESULT_RENDER_FAILED,
        FinalErrorCodeV1.TASK_RESULT_COMMIT_FAILED,
        FinalErrorCodeV1.TASK_RESULT_COMMIT_CONFLICT,
        FinalErrorCodeV1.TASK_RESULT_CONTRACT_MISMATCH,
        FinalErrorCodeV1.TASK_DEADLINE_EXCEEDED,
    }
)

FINAL_CONTROL_RESULT_CODES: frozenset[FinalErrorCodeV1] = frozenset(
    {
        FinalErrorCodeV1.FINAL_SYNTHESIS_SNAPSHOT_STALE,
        FinalErrorCodeV1.FINAL_CANDIDATE_REPAIR_REQUIRED,
    }
)

_FORWARD_ORDER: tuple[FinalizationSubstateV1, ...] = (
    FinalizationSubstateV1.CREATED,
    FinalizationSubstateV1.SNAPSHOT_BOUND,
    FinalizationSubstateV1.MODEL_RUNNING,
    FinalizationSubstateV1.CANDIDATE_STAGED,
    FinalizationSubstateV1.VALIDATING,
    FinalizationSubstateV1.REPAIRING,
    FinalizationSubstateV1.ACCEPTED,
    FinalizationSubstateV1.ASSEMBLING,
    FinalizationSubstateV1.COMMITTING,
    FinalizationSubstateV1.COMMITTED,
)

_RESUMPTION_BY_STATE: dict[FinalModelCallStateV1, FinalModelCallResumptionV1] = {
    FinalModelCallStateV1.COMPLETED: FinalModelCallResumptionV1.REUSE_COMPLETED,
    FinalModelCallStateV1.TRANSIENT_NO_DELTA: FinalModelCallResumptionV1.RETRY_SAME_BINDING,
    FinalModelCallStateV1.STREAMING_INTERRUPTED: FinalModelCallResumptionV1.RECONCILE_FIRST,
    FinalModelCallStateV1.UNKNOWN: FinalModelCallResumptionV1.RECONCILE_FIRST,
}


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC_DATETIME_REQUIRED")
    return value.astimezone(UTC)


def is_terminal_substate(substate: FinalizationSubstateV1) -> bool:
    return substate in FINAL_TERMINAL_SUBSTATES


def is_terminal_error_code(code: FinalErrorCodeV1) -> bool:
    return code in FINAL_TERMINAL_ERROR_CODES


def advance_substate(
    current: FinalizationSubstateV1, target: FinalizationSubstateV1
) -> FinalizationSubstateV1:
    """只前进的子状态机：终态吸收，回退/同态失败关闭；前向可跳步。"""

    if is_terminal_substate(current):
        raise ValueError("FINALIZATION_SUBSTATE_TERMINAL")
    if is_terminal_substate(target):
        return target
    if _FORWARD_ORDER.index(target) <= _FORWARD_ORDER.index(current):
        raise ValueError("FINALIZATION_SUBSTATE_BACKWARD")
    return target


def deadline_verdict(*, clock: datetime, deadline_at: datetime) -> DeadlineVerdictV1:
    """`clock >= deadline_at` 即超期；即使候选合法也只能 FAILED + TASK_DEADLINE_EXCEEDED。"""

    if _require_utc(clock) >= _require_utc(deadline_at):
        return "TASK_DEADLINE_EXCEEDED"
    return "WITHIN_DEADLINE"


def snapshot_rebuild_verdict(completed_rebuilds: int) -> SnapshotRebuildVerdictV1:
    """连续 Snapshot 重建最多 3 次，第 4 次以 FINAL_SYNTHESIS_INPUT_UNSTABLE 失败。"""

    if completed_rebuilds < 0:
        raise ValueError("SNAPSHOT_REBUILD_COUNT_NEGATIVE")
    if completed_rebuilds < FINAL_SNAPSHOT_REBUILD_LIMIT:
        return "ALLOWED"
    return "FINAL_SYNTHESIS_INPUT_UNSTABLE"


def classify_model_call_resumption(
    state: FinalModelCallStateV1,
) -> FinalModelCallResumptionV1:
    """已 COMPLETED 直接复用；无 Delta 的瞬态 429/503 按同一精确 Binding 重试；其余先对账。"""

    return _RESUMPTION_BY_STATE[state]


def cancel_vs_commit_terminal(winner: CancelCommitWinnerV1) -> FinalizationSubstateV1:
    """线性化点唯一：取消先到则提交失败并 CANCELLED，提交先到则后续取消返回已完成。"""

    if winner == "CANCEL":
        return FinalizationSubstateV1.CANCELLED
    return FinalizationSubstateV1.COMMITTED


def final_commit_block(
    *, deadline_exceeded: bool, cancelled: bool
) -> FinalizationSubstateV1 | None:
    """提交前复核的阻塞终态：取消优先于超期；无阻塞返回 None。"""

    if cancelled:
        return FinalizationSubstateV1.CANCELLED
    if deadline_exceeded:
        return FinalizationSubstateV1.FAILED
    return None


__all__ = [
    "FINAL_CONTROL_RESULT_CODES",
    "FINAL_CONTROL_TRANSACTION_ATTEMPTS",
    "FINAL_MODEL_REQUEST_ATTEMPTS",
    "FINAL_SNAPSHOT_REBUILD_LIMIT",
    "FINAL_TERMINAL_ERROR_CODES",
    "FINAL_TERMINAL_SUBSTATES",
    "CancelCommitWinnerV1",
    "DeadlineVerdictV1",
    "FinalErrorCodeV1",
    "FinalModelCallResumptionV1",
    "FinalModelCallStateV1",
    "FinalizationSubstateV1",
    "SnapshotRebuildVerdictV1",
    "advance_substate",
    "cancel_vs_commit_terminal",
    "classify_model_call_resumption",
    "deadline_verdict",
    "final_commit_block",
    "is_terminal_error_code",
    "is_terminal_substate",
    "snapshot_rebuild_verdict",
]
