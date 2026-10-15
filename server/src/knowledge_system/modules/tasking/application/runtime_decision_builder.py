"""DEV-06 决策快照纯投影：`RuntimeDecisionFactsV1` → `RuntimeDecisionSnapshotV1`（零 LLM、零 I/O）。

把上一片切的循环级收敛动作（`RuntimeConvergenceActionV1`）编码进决策快照的 `replan_admission` /
`replan_budget_complete` / `finalization_reason` / `fail_reason`，其余 32 个事实字段原样透传，
交给既有零 LLM `evaluate_runtime_decision` 按固定优先序派发唯一指令。

本切片为纯投影，不读 PG；`RuntimeDecisionFactsV1` 的填充由后续 PG 只读仓库负责。
"""

from __future__ import annotations

from typing import Literal

from knowledge_system.modules.tasking.application.runtime_convergence import (
    RuntimeConvergenceActionV1,
)
from knowledge_system.modules.tasking.application.runtime_decision_facts import (
    RuntimeDecisionFactsV1,
    map_task_status,
)
from knowledge_system.modules.tasking.domain.runtime_decision import (
    DecisionReasonV1,
    FinalOutcomeKindV1,
    ReplanAdmissionV1,
    RuntimeDecisionSnapshotV1,
    freeze_runtime_decision_snapshot,
)

_SNAPSHOT_CONTRACT: Literal["runtime_decision_snapshot_v1"] = "runtime_decision_snapshot_v1"


def _derive_replan(
    facts: RuntimeDecisionFactsV1,
) -> tuple[ReplanAdmissionV1, bool]:
    """无进展收敛动作 → `replan_admission` + `replan_budget_complete`。

    - `replan_required`（硬信号）优先于收敛动作 → REQUIRED。
    - `REPLAN`：上一片切已消费一次预算、确有替代路径 → ELIGIBLE。
    - `CONVERGE`：预算已定论（耗尽/强制收敛）→ BLOCKED。
    - `CONTINUE_LOOP`：预算未定论 → NOT_NEEDED。
    `replan_budget_complete` = 「重规划预算已定论」：仅 `CONTINUE_LOOP` 时为 False。
    """

    if facts.replan_required:
        return ReplanAdmissionV1.REQUIRED, True
    if facts.convergence_action is RuntimeConvergenceActionV1.REPLAN:
        return ReplanAdmissionV1.ELIGIBLE, True
    if facts.convergence_action is RuntimeConvergenceActionV1.CONVERGE:
        return ReplanAdmissionV1.BLOCKED, True
    return ReplanAdmissionV1.NOT_NEEDED, False


def _derive_terminal(
    facts: RuntimeDecisionFactsV1,
) -> tuple[
    DecisionReasonV1 | None,
    FinalOutcomeKindV1 | None,
    DecisionReasonV1 | None,
    bool,
]:
    """收敛/完成事实 → `fail_reason` / `final_outcome_kind` / `finalization_reason` / 强制合并。

    CONVERGE 分支优先于 `required_criteria_satisfied`（收敛是硬终态）。返回四元组：
    `(fail_reason, final_outcome_kind, finalization_reason, force_contributions_merged)`。
    """

    if facts.convergence_action is RuntimeConvergenceActionV1.CONVERGE:
        if facts.replan_required:
            return DecisionReasonV1.REPLAN_REQUIRED_FAILED, None, None, False
        if facts.has_partial_results:
            return (
                None,
                FinalOutcomeKindV1.PARTIAL,
                DecisionReasonV1.PARTIAL_RESULT_ONLY,
                True,
            )
        return DecisionReasonV1.CONTROL_DEPENDENCY_EXHAUSTED, None, None, False
    if facts.required_criteria_satisfied:
        return (
            None,
            FinalOutcomeKindV1.COMPLETE,
            DecisionReasonV1.REQUIRED_CRITERION_SATISFIED,
            False,
        )
    return None, None, None, False


def build_runtime_decision_snapshot(
    facts: RuntimeDecisionFactsV1,
) -> RuntimeDecisionSnapshotV1:
    """事实契约 → 冻结决策快照；`task_state`/`replan_*`/终态信号为派生，其余原样透传。"""

    replan_admission, replan_budget_complete = _derive_replan(facts)
    fail_reason, outcome_kind, finalization_reason, force_merge = _derive_terminal(facts)
    contributions_merged = facts.contributions_merged or force_merge

    snapshot = RuntimeDecisionSnapshotV1(
        contract_version=_SNAPSHOT_CONTRACT,
        task_id=facts.task_id,
        task_row_version=facts.task_row_version,
        event_high_watermark=facts.event_high_watermark,
        task_state=map_task_status(facts.db_status),
        input_id=facts.input_id,
        input_version=facts.input_version,
        plan_version=facts.plan_version,
        work_version=facts.work_version,
        execution_generation=facts.execution_generation,
        cancellation_generation=facts.cancellation_generation,
        deadline_at=facts.deadline_at,
        terminal_committed=facts.terminal_committed,
        runtime_close_ready=facts.runtime_close_ready,
        has_active_execution_unit=facts.has_active_execution_unit,
        has_unresolved_unknown_effect=facts.has_unresolved_unknown_effect,
        has_pending_cancellation=facts.has_pending_cancellation,
        cancellation_fence_required=facts.cancellation_fence_required,
        cancellation_terminal_kind=facts.cancellation_terminal_kind,
        has_blocking_unknown_effect=facts.has_blocking_unknown_effect,
        reconciliation_ready=facts.reconciliation_ready,
        blocking_effect_refs=facts.blocking_effect_refs,
        next_reconcile_at=facts.next_reconcile_at,
        fail_reason=fail_reason,
        blocking_issue=facts.blocking_issue,
        replan_admission=replan_admission,
        replan_budget_complete=replan_budget_complete,
        has_active_replan=facts.has_active_replan,
        snapshot_version_stable=facts.snapshot_version_stable,
        has_unaffected_ready_items=facts.has_unaffected_ready_items,
        retryable_items=facts.retryable_items,
        final_outcome_kind=outcome_kind,
        finalization_reason=finalization_reason,
        contributions_merged=contributions_merged,
        has_ready_items=facts.has_ready_items,
        ready_set_ref=facts.ready_set_ref,
        scheduling_state_valid=facts.scheduling_state_valid,
        wake_sources=facts.wake_sources,
    )
    return freeze_runtime_decision_snapshot(snapshot)


class RuntimeDecisionContextBuilder:
    """决策快照构建器（docstring 点名的交付物）；本切片为纯投影，PG 事实读取留待后续只读仓库。"""

    def build(self, facts: RuntimeDecisionFactsV1) -> RuntimeDecisionSnapshotV1:
        return build_runtime_decision_snapshot(facts)


__all__ = ["RuntimeDecisionContextBuilder", "build_runtime_decision_snapshot"]
