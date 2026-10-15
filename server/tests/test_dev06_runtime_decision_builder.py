"""DEV-06 决策快照纯投影的离线验收（零 LLM、零 I/O）。

覆盖 `map_task_status`（DB 状态串 → 领域运行时状态，未知名 fail-closed）、
`RuntimeDecisionFactsV1` 事实契约（extra 禁止 / 白名单 / UTC / Replan 预算自洽），以及
`build_runtime_decision_snapshot` 把 `RuntimeConvergenceActionV1` 编码进 `replan_admission` /
`replan_budget_complete` / 终态信号、其余事实原样透传，交给既有 `evaluate_runtime_decision`
派发唯一指令。
"""

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import ValidationError

from knowledge_system.modules.tasking.application.runtime_convergence import (
    RuntimeConvergenceActionV1,
)
from knowledge_system.modules.tasking.application.runtime_decision_builder import (
    RuntimeDecisionContextBuilder,
    build_runtime_decision_snapshot,
)
from knowledge_system.modules.tasking.application.runtime_decision_facts import (
    DB_TASK_STATUSES,
    RuntimeDecisionFactsV1,
    map_task_status,
)
from knowledge_system.modules.tasking.domain.runtime_decision import (
    DecisionReasonV1,
    FinalOutcomeKindV1,
    ReplanAdmissionV1,
    TaskRuntimeStateV1,
    evaluate_runtime_decision,
)
from knowledge_system.modules.tasking.domain.runtime_progress import NoProgressStateV1

ZERO_SHA = "0" * 64
TASK_ID = UUID("11111111-1111-4111-8111-111111111111")
INPUT_ID = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
DEADLINE = NOW + timedelta(hours=6)


def _facts_payload(**overrides) -> dict:
    base = dict(
        task_id=TASK_ID,
        task_row_version=1,
        event_high_watermark=0,
        db_status="RUNNING",
        input_id=INPUT_ID,
        input_version=1,
        plan_version=1,
        work_version=1,
        execution_generation=0,
        cancellation_generation=0,
        deadline_at=DEADLINE,
        terminal_committed=False,
        runtime_close_ready=False,
        has_active_execution_unit=False,
        has_unresolved_unknown_effect=False,
        has_pending_cancellation=False,
        cancellation_fence_required=False,
        has_blocking_unknown_effect=False,
        reconciliation_ready=False,
        blocking_effect_refs=(),
        has_active_replan=False,
        snapshot_version_stable=True,
        has_unaffected_ready_items=False,
        retryable_items=(),
        contributions_merged=False,
        has_ready_items=False,
        scheduling_state_valid=True,
        wake_sources=(),
        convergence_action=RuntimeConvergenceActionV1.CONTINUE_LOOP,
        no_progress_state=NoProgressStateV1(),
    )
    base.update(overrides)
    return base


def facts(**overrides) -> RuntimeDecisionFactsV1:
    return RuntimeDecisionFactsV1.model_validate(_facts_payload(**overrides))


def replan_state(**overrides) -> NoProgressStateV1:
    base = dict(episode_replan_used=True, task_replan_count=1)
    base.update(overrides)
    return NoProgressStateV1.model_validate(base)


def replan_facts(**overrides) -> RuntimeDecisionFactsV1:
    return facts(
        convergence_action=RuntimeConvergenceActionV1.REPLAN,
        no_progress_state=replan_state(),
        **overrides,
    )


class MapTaskStatusTests(unittest.TestCase):
    def test_active_statuses_map_to_running(self) -> None:
        for status in (
            "QUEUED",
            "PLANNING",
            "RUNNING",
            "REPLANNING",
            "WAITING_USER_INPUT",
            "WAITING_APPROVAL",
            "RETRY_WAIT",
        ):
            self.assertEqual(map_task_status(status), TaskRuntimeStateV1.RUNNING, status)

    def test_terminalish_statuses_map_to_self(self) -> None:
        self.assertEqual(map_task_status("FINALIZING"), TaskRuntimeStateV1.FINALIZING)
        self.assertEqual(map_task_status("CANCEL_REQUESTED"), TaskRuntimeStateV1.CANCEL_REQUESTED)
        self.assertEqual(map_task_status("COMPLETED"), TaskRuntimeStateV1.COMPLETED)
        self.assertEqual(map_task_status("FAILED"), TaskRuntimeStateV1.FAILED)
        self.assertEqual(map_task_status("CANCELLED"), TaskRuntimeStateV1.CANCELLED)

    def test_unknown_status_raises(self) -> None:
        with self.assertRaises(ValueError):
            map_task_status("BOGUS")

    def test_whitelist_is_complete(self) -> None:
        self.assertEqual(len(DB_TASK_STATUSES), 12)
        self.assertTrue(all(map_task_status(s) in TaskRuntimeStateV1 for s in DB_TASK_STATUSES))


class RuntimeDecisionFactsV1ContractTests(unittest.TestCase):
    def test_extra_field_forbidden(self) -> None:
        with self.assertRaises(ValidationError):
            RuntimeDecisionFactsV1.model_validate(_facts_payload(bogus=1))

    def test_db_status_whitelist(self) -> None:
        with self.assertRaises(ValidationError):
            facts(db_status="BOGUS")

    def test_deadline_requires_utc(self) -> None:
        with self.assertRaises(ValidationError):
            facts(deadline_at=datetime(2026, 10, 14, 12, 0, 0))

    def test_defaults(self) -> None:
        f = facts()
        self.assertFalse(f.replan_required)
        self.assertFalse(f.has_partial_results)
        self.assertFalse(f.required_criteria_satisfied)
        self.assertIsNone(f.cancellation_terminal_kind)
        self.assertIsNone(f.next_reconcile_at)
        self.assertIsNone(f.blocking_issue)
        self.assertIsNone(f.ready_set_ref)

    def test_replan_requires_marked_budget(self) -> None:
        # REPLAN 但 Episode 未标记消费 → 拒绝。
        with self.assertRaises(ValidationError):
            facts(convergence_action=RuntimeConvergenceActionV1.REPLAN)
        # REPLAN 但 task_replan_count=0（越界）→ 拒绝。
        with self.assertRaises(ValidationError):
            facts(
                convergence_action=RuntimeConvergenceActionV1.REPLAN,
                no_progress_state=NoProgressStateV1(episode_replan_used=True, task_replan_count=0),
            )
        # 合法预算状态 → 通过。
        f = facts(
            convergence_action=RuntimeConvergenceActionV1.REPLAN,
            no_progress_state=replan_state(),
        )
        self.assertEqual(f.convergence_action, RuntimeConvergenceActionV1.REPLAN)


class DerivationTests(unittest.TestCase):
    def test_continue_loop_is_not_needed(self) -> None:
        snap = build_runtime_decision_snapshot(facts())
        self.assertEqual(snap.replan_admission, ReplanAdmissionV1.NOT_NEEDED)
        self.assertFalse(snap.replan_budget_complete)
        self.assertEqual(snap.task_state, TaskRuntimeStateV1.RUNNING)
        self.assertIsNone(snap.fail_reason)
        self.assertIsNone(snap.final_outcome_kind)
        self.assertIsNone(snap.finalization_reason)

    def test_replan_is_eligible(self) -> None:
        snap = build_runtime_decision_snapshot(replan_facts())
        self.assertEqual(snap.replan_admission, ReplanAdmissionV1.ELIGIBLE)
        self.assertTrue(snap.replan_budget_complete)

    def test_replan_required_overrides_action(self) -> None:
        snap = build_runtime_decision_snapshot(facts(replan_required=True))
        self.assertEqual(snap.replan_admission, ReplanAdmissionV1.REQUIRED)
        self.assertTrue(snap.replan_budget_complete)

    def test_converge_without_partial_fails_closed(self) -> None:
        snap = build_runtime_decision_snapshot(
            facts(convergence_action=RuntimeConvergenceActionV1.CONVERGE)
        )
        self.assertEqual(snap.replan_admission, ReplanAdmissionV1.BLOCKED)
        self.assertTrue(snap.replan_budget_complete)
        self.assertEqual(snap.fail_reason, DecisionReasonV1.CONTROL_DEPENDENCY_EXHAUSTED)
        self.assertIsNone(snap.final_outcome_kind)
        self.assertIsNone(snap.finalization_reason)

    def test_converge_with_partial(self) -> None:
        snap = build_runtime_decision_snapshot(
            facts(convergence_action=RuntimeConvergenceActionV1.CONVERGE, has_partial_results=True)
        )
        self.assertEqual(snap.final_outcome_kind, FinalOutcomeKindV1.PARTIAL)
        self.assertEqual(snap.finalization_reason, DecisionReasonV1.PARTIAL_RESULT_ONLY)
        self.assertTrue(snap.contributions_merged)
        self.assertIsNone(snap.fail_reason)

    def test_converge_replan_required_takes_priority(self) -> None:
        snap = build_runtime_decision_snapshot(
            facts(
                convergence_action=RuntimeConvergenceActionV1.CONVERGE,
                replan_required=True,
                has_partial_results=True,
            )
        )
        self.assertEqual(snap.fail_reason, DecisionReasonV1.REPLAN_REQUIRED_FAILED)
        self.assertIsNone(snap.final_outcome_kind)

    def test_required_criteria_satisfied_completes(self) -> None:
        snap = build_runtime_decision_snapshot(facts(required_criteria_satisfied=True))
        self.assertEqual(snap.final_outcome_kind, FinalOutcomeKindV1.COMPLETE)
        self.assertEqual(snap.finalization_reason, DecisionReasonV1.REQUIRED_CRITERION_SATISFIED)
        self.assertIsNone(snap.fail_reason)
        self.assertFalse(snap.contributions_merged)


class EndToEndControllerTests(unittest.TestCase):
    def test_replan_dispatches_start_replan(self) -> None:
        snap = build_runtime_decision_snapshot(replan_facts())
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(decision.directive.action, "START_REPLAN")
        self.assertEqual(decision.directive.admission, "ELIGIBLE")

    def test_converge_partial_dispatches_final_synthesis(self) -> None:
        snap = build_runtime_decision_snapshot(
            facts(convergence_action=RuntimeConvergenceActionV1.CONVERGE, has_partial_results=True)
        )
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(decision.directive.action, "START_FINAL_SYNTHESIS")
        self.assertEqual(decision.directive.outcome_kind, FinalOutcomeKindV1.PARTIAL)

    def test_converge_no_partial_dispatches_fail_task(self) -> None:
        snap = build_runtime_decision_snapshot(
            facts(convergence_action=RuntimeConvergenceActionV1.CONVERGE)
        )
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(decision.directive.action, "FAIL_TASK")
        self.assertEqual(
            decision.directive.fail_reason, DecisionReasonV1.CONTROL_DEPENDENCY_EXHAUSTED
        )

    def test_completed_dispatches_close_runtime(self) -> None:
        snap = build_runtime_decision_snapshot(
            facts(db_status="COMPLETED", terminal_committed=True, runtime_close_ready=True)
        )
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(decision.directive.action, "CLOSE_RUNTIME")

    def test_deterministic_digest(self) -> None:
        first = build_runtime_decision_snapshot(facts())
        second = build_runtime_decision_snapshot(facts())
        self.assertEqual(first.snapshot_digest, second.snapshot_digest)
        self.assertEqual(len(first.snapshot_digest), 64)

    def test_digest_changes_with_facts(self) -> None:
        first = build_runtime_decision_snapshot(facts())
        second = build_runtime_decision_snapshot(facts(task_row_version=2))
        self.assertNotEqual(first.snapshot_digest, second.snapshot_digest)
        self.assertRegex(first.snapshot_digest, r"^[0-9a-f]{64}$")


class RuntimeDecisionContextBuilderTests(unittest.TestCase):
    def test_builder_delegates_to_function(self) -> None:
        builder = RuntimeDecisionContextBuilder()
        self.assertEqual(
            builder.build(facts()).snapshot_digest,
            build_runtime_decision_snapshot(facts()).snapshot_digest,
        )


if __name__ == "__main__":
    unittest.main()
