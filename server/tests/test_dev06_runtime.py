"""DEV-06：零LLM RuntimeDecisionController 离线验收（十类指令优先序、前提与决策契约）。"""

import unittest
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError

from knowledge_system.modules.tasking.domain.core_types import VersionedRefV1
from knowledge_system.modules.tasking.domain.runtime_decision import (
    CONTROLLER_PROFILE_VERSION,
    RUNTIME_DECISION_NAMESPACE,
    RUNTIME_DIRECTIVE_ADAPTER,
    CloseRuntimeDirectiveV1,
    DecisionReasonV1,
    FailTaskDirectiveV1,
    FinalOutcomeKindV1,
    ReplanAdmissionV1,
    RetryablePlanItemV1,
    RuntimeBlockingIssueKindV1,
    RuntimeBlockingIssueV1,
    RuntimeDecisionReceiptOutcomeV1,
    RuntimeDecisionReceiptV1,
    RuntimeDecisionRefV1,
    RuntimeDecisionSnapshotV1,
    RuntimeDecisionV1,
    RuntimeDirectiveActionV1,
    TaskRuntimeStateV1,
    WaitEventDirectiveV1,
    WakeSourceV1,
    decision_id,
    evaluate_runtime_decision,
    freeze_runtime_decision_snapshot,
    snapshot_digest_for,
)

ZERO_SHA = "0" * 64
TASK_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
INPUT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PLAN_ITEM_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
EFFECT_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
READY_SET_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")


def utc_dt(hour: int = 12) -> datetime:
    return datetime(2026, 10, 8, hour, 0, 0, tzinfo=UTC)


def versioned_ref(ref_id: UUID) -> VersionedRefV1:
    return VersionedRefV1(ref_id=ref_id, version=1, integrity_digest=ZERO_SHA)


def blocking_issue() -> RuntimeBlockingIssueV1:
    return RuntimeBlockingIssueV1(
        issue_kind=RuntimeBlockingIssueKindV1.REQUIRED_PARAMETER_MISSING,
        issue_fingerprint=ZERO_SHA,
        refs=(),
    )


def retryable_item() -> RetryablePlanItemV1:
    return RetryablePlanItemV1(
        plan_item_id=PLAN_ITEM_ID,
        next_iteration_no=2,
        retry_reason="BUSINESS_RETRYABLE_FAILURE",
    )


def wake_source() -> WakeSourceV1:
    return WakeSourceV1(
        kind="BUSINESS_BACKOFF",
        refs=(),
        next_wakeup_at=utc_dt(13),
    )


def snapshot(**overrides) -> RuntimeDecisionSnapshotV1:
    base = dict(
        contract_version="runtime_decision_snapshot_v1",
        task_id=TASK_ID,
        task_row_version=1,
        event_high_watermark=0,
        task_state=TaskRuntimeStateV1.RUNNING,
        input_id=INPUT_ID,
        input_version=1,
        plan_version=1,
        work_version=1,
        execution_generation=0,
        cancellation_generation=0,
        deadline_at=utc_dt(18),
        terminal_committed=False,
        runtime_close_ready=False,
        has_active_execution_unit=False,
        has_unresolved_unknown_effect=False,
        has_pending_cancellation=False,
        cancellation_fence_required=False,
        cancellation_terminal_kind=None,
        has_blocking_unknown_effect=False,
        reconciliation_ready=False,
        blocking_effect_refs=(),
        next_reconcile_at=None,
        fail_reason=None,
        blocking_issue=None,
        replan_admission=ReplanAdmissionV1.NOT_NEEDED,
        replan_budget_complete=False,
        has_active_replan=False,
        snapshot_version_stable=True,
        has_unaffected_ready_items=False,
        retryable_items=(),
        final_outcome_kind=None,
        finalization_reason=None,
        contributions_merged=False,
        has_ready_items=False,
        ready_set_ref=None,
        scheduling_state_valid=True,
        wake_sources=(),
    )
    base.update(overrides)
    return RuntimeDecisionSnapshotV1.model_validate(base)


def action_of(decision: RuntimeDecisionV1) -> str:
    return decision.directive.action


class NamespaceAndIdTests(unittest.TestCase):
    def test_namespace_frozen(self) -> None:
        self.assertIsInstance(RUNTIME_DECISION_NAMESPACE, UUID)
        self.assertNotEqual(RUNTIME_DECISION_NAMESPACE, UUID(int=0))

    def test_profile_version_frozen(self) -> None:
        self.assertEqual(CONTROLLER_PROFILE_VERSION, "runtime-decision-controller-v1")

    def test_snapshot_digest_is_stable_64_hex(self) -> None:
        digest = snapshot_digest_for(snapshot())
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, snapshot_digest_for(snapshot()))

    def test_snapshot_digest_ignores_its_own_field(self) -> None:
        plain = snapshot_digest_for(snapshot())
        prefilled = snapshot(snapshot_digest=ZERO_SHA)
        self.assertEqual(plain, snapshot_digest_for(prefilled))

    def test_freeze_returns_same_identity(self) -> None:
        frozen = freeze_runtime_decision_snapshot(snapshot())
        self.assertEqual(frozen.task_id, TASK_ID)
        self.assertEqual(len(frozen.snapshot_digest), 64)

    def test_decision_id_deterministic(self) -> None:
        digest = snapshot_digest_for(snapshot())
        self.assertEqual(decision_id(TASK_ID, digest), decision_id(TASK_ID, digest))

    def test_decision_id_changes_with_snapshot(self) -> None:
        first = decision_id(TASK_ID, snapshot_digest_for(snapshot()))
        second = decision_id(TASK_ID, snapshot_digest_for(snapshot(task_row_version=2)))
        self.assertNotEqual(first, second)

    def test_same_basis_same_id_and_digest(self) -> None:
        first = evaluate_runtime_decision(snapshot(wake_sources=(wake_source(),)), 1)
        second = evaluate_runtime_decision(snapshot(wake_sources=(wake_source(),)), 1)
        self.assertEqual(first.decision_id, second.decision_id)
        self.assertEqual(first.decision_digest, second.decision_digest)
        self.assertEqual(len(first.decision_digest), 64)


class DirectivePriorityTests(unittest.TestCase):
    def test_close_runtime_wins_first(self) -> None:
        snap = snapshot(
            task_state=TaskRuntimeStateV1.COMPLETED,
            terminal_committed=True,
            runtime_close_ready=True,
        )
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(action_of(decision), "CLOSE_RUNTIME")
        self.assertIsInstance(decision.directive, CloseRuntimeDirectiveV1)
        self.assertEqual(decision.directive.terminal_state, TaskRuntimeStateV1.COMPLETED)

    def test_advance_cancellation(self) -> None:
        snap = snapshot(
            task_state=TaskRuntimeStateV1.CANCEL_REQUESTED,
            has_pending_cancellation=True,
            cancellation_terminal_kind="CANCELLED",
        )
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(action_of(decision), "ADVANCE_CANCELLATION")
        self.assertEqual(decision.directive.terminal_kind, "CANCELLED")

    def test_wait_reconciliation(self) -> None:
        snap = snapshot(
            has_blocking_unknown_effect=True,
            reconciliation_ready=True,
            blocking_effect_refs=(versioned_ref(EFFECT_ID),),
            next_reconcile_at=utc_dt(12),
        )
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(action_of(decision), "WAIT_RECONCILIATION")
        self.assertEqual(decision.directive.effect_refs[0].ref_id, EFFECT_ID)

    def test_fail_task(self) -> None:
        snap = snapshot(fail_reason=DecisionReasonV1.INTEGRITY_CORRUPTION)
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(action_of(decision), "FAIL_TASK")
        self.assertIsInstance(decision.directive, FailTaskDirectiveV1)
        self.assertEqual(decision.reasons, (DecisionReasonV1.INTEGRITY_CORRUPTION,))

    def test_request_user_input(self) -> None:
        snap = snapshot(blocking_issue=blocking_issue())
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(action_of(decision), "REQUEST_USER_INPUT")
        self.assertEqual(decision.reasons, (DecisionReasonV1.REQUIRED_PARAMETER_MISSING,))

    def test_start_replan_required(self) -> None:
        snap = snapshot(
            replan_admission=ReplanAdmissionV1.REQUIRED,
            replan_budget_complete=True,
        )
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(action_of(decision), "START_REPLAN")
        self.assertEqual(decision.reasons, (DecisionReasonV1.REPLAN_REQUIRED,))

    def test_start_replan_eligible_does_not_preempt_ready_items(self) -> None:
        snap = snapshot(
            replan_admission=ReplanAdmissionV1.ELIGIBLE,
            replan_budget_complete=True,
            has_unaffected_ready_items=True,
            has_ready_items=True,
            ready_set_ref=versioned_ref(READY_SET_ID),
        )
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(action_of(decision), "SCHEDULE_READY_SET")

    def test_retry_plan_item(self) -> None:
        snap = snapshot(retryable_items=(retryable_item(),))
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(action_of(decision), "RETRY_PLAN_ITEM")
        self.assertEqual(decision.reasons, (DecisionReasonV1.BUSINESS_RETRYABLE_FAILURE,))

    def test_start_final_synthesis(self) -> None:
        snap = snapshot(
            final_outcome_kind=FinalOutcomeKindV1.COMPLETE,
            finalization_reason=DecisionReasonV1.REQUIRED_CRITERION_SATISFIED,
            contributions_merged=True,
        )
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(action_of(decision), "START_FINAL_SYNTHESIS")
        self.assertEqual(decision.directive.outcome_kind, FinalOutcomeKindV1.COMPLETE)

    def test_schedule_ready_set(self) -> None:
        snap = snapshot(
            has_ready_items=True,
            ready_set_ref=versioned_ref(READY_SET_ID),
        )
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(action_of(decision), "SCHEDULE_READY_SET")

    def test_wait_event(self) -> None:
        snap = snapshot(wake_sources=(wake_source(),))
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(action_of(decision), "WAIT_EVENT")
        self.assertIsInstance(decision.directive, WaitEventDirectiveV1)

    def test_deadlock_without_wake_source(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_runtime_decision(snapshot(), 1)

    def test_fail_task_beats_request_user_input(self) -> None:
        snap = snapshot(
            fail_reason=DecisionReasonV1.SAFETY_FAIL_CLOSED,
            blocking_issue=blocking_issue(),
        )
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(action_of(decision), "FAIL_TASK")

    def test_reconciliation_beats_fail_task(self) -> None:
        snap = snapshot(
            has_blocking_unknown_effect=True,
            reconciliation_ready=True,
            next_reconcile_at=utc_dt(12),
            fail_reason=DecisionReasonV1.REQUIRED_STATE_UNRECOVERABLE,
        )
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(action_of(decision), "WAIT_RECONCILIATION")


class PreconditionTests(unittest.TestCase):
    def test_close_runtime_requires_no_active_execution(self) -> None:
        snap = snapshot(
            task_state=TaskRuntimeStateV1.COMPLETED,
            terminal_committed=True,
            runtime_close_ready=True,
            has_active_execution_unit=True,
            wake_sources=(wake_source(),),
        )
        self.assertNotEqual(action_of(evaluate_runtime_decision(snap, 1)), "CLOSE_RUNTIME")

    def test_close_runtime_requires_terminal_committed(self) -> None:
        snap = snapshot(
            task_state=TaskRuntimeStateV1.COMPLETED,
            runtime_close_ready=True,
            wake_sources=(wake_source(),),
        )
        self.assertNotEqual(action_of(evaluate_runtime_decision(snap, 1)), "CLOSE_RUNTIME")

    def test_replan_requires_budget_complete(self) -> None:
        snap = snapshot(
            replan_admission=ReplanAdmissionV1.REQUIRED,
            replan_budget_complete=False,
            wake_sources=(wake_source(),),
        )
        self.assertNotEqual(action_of(evaluate_runtime_decision(snap, 1)), "START_REPLAN")

    def test_replan_requires_version_stable(self) -> None:
        snap = snapshot(
            replan_admission=ReplanAdmissionV1.REQUIRED,
            replan_budget_complete=True,
            snapshot_version_stable=False,
            wake_sources=(wake_source(),),
        )
        self.assertNotEqual(action_of(evaluate_runtime_decision(snap, 1)), "START_REPLAN")

    def test_replan_requires_no_active_replan(self) -> None:
        snap = snapshot(
            replan_admission=ReplanAdmissionV1.REQUIRED,
            replan_budget_complete=True,
            has_active_replan=True,
            wake_sources=(wake_source(),),
        )
        self.assertNotEqual(action_of(evaluate_runtime_decision(snap, 1)), "START_REPLAN")

    def test_final_requires_merged_contributions(self) -> None:
        snap = snapshot(
            final_outcome_kind=FinalOutcomeKindV1.PARTIAL,
            finalization_reason=DecisionReasonV1.PARTIAL_RESULT_ONLY,
            contributions_merged=False,
            wake_sources=(wake_source(),),
        )
        self.assertNotEqual(action_of(evaluate_runtime_decision(snap, 1)), "START_FINAL_SYNTHESIS")

    def test_final_requires_no_blocking_issue(self) -> None:
        snap = snapshot(
            final_outcome_kind=FinalOutcomeKindV1.COMPLETE,
            finalization_reason=DecisionReasonV1.REQUIRED_CRITERION_SATISFIED,
            contributions_merged=True,
            blocking_issue=blocking_issue(),
        )
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(action_of(decision), "REQUEST_USER_INPUT")

    def test_schedule_requires_running(self) -> None:
        snap = snapshot(
            task_state=TaskRuntimeStateV1.FINALIZING,
            has_ready_items=True,
            ready_set_ref=versioned_ref(READY_SET_ID),
            wake_sources=(wake_source(),),
        )
        self.assertNotEqual(action_of(evaluate_runtime_decision(snap, 1)), "SCHEDULE_READY_SET")

    def test_schedule_requires_valid_state(self) -> None:
        snap = snapshot(
            has_ready_items=True,
            ready_set_ref=versioned_ref(READY_SET_ID),
            scheduling_state_valid=False,
            wake_sources=(wake_source(),),
        )
        self.assertNotEqual(action_of(evaluate_runtime_decision(snap, 1)), "SCHEDULE_READY_SET")

    def test_retry_blocked_by_blocking_effect(self) -> None:
        snap = snapshot(
            retryable_items=(retryable_item(),),
            has_blocking_unknown_effect=True,
            reconciliation_ready=True,
            next_reconcile_at=utc_dt(12),
        )
        decision = evaluate_runtime_decision(snap, 1)
        self.assertEqual(action_of(decision), "WAIT_RECONCILIATION")


class ContractTests(unittest.TestCase):
    def test_decision_reasons_mismatch_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RuntimeDecisionV1(
                contract_version="runtime_decision_v1",
                decision_id=UUID(int=1),
                decision_sequence=1,
                snapshot_digest=ZERO_SHA,
                controller_profile_version=CONTROLLER_PROFILE_VERSION,
                directive=CloseRuntimeDirectiveV1(
                    action="CLOSE_RUNTIME", terminal_state=TaskRuntimeStateV1.COMPLETED
                ),
                reasons=(DecisionReasonV1.READY_ITEMS_AVAILABLE,),
                basis_task_row_version=1,
                basis_event_high_watermark=0,
                issued_task_row_version=1,
                input_id=INPUT_ID,
                input_version=1,
                plan_version=1,
                work_version=1,
                execution_generation=0,
                cancellation_generation=0,
                decision_digest=ZERO_SHA,
            )

    def test_decision_extra_field_forbidden(self) -> None:
        decision = evaluate_runtime_decision(snapshot(wake_sources=(wake_source(),)), 1)
        with self.assertRaises(ValidationError):
            RuntimeDecisionV1.model_validate({**decision.model_dump(), "forged": "value"})

    def test_fail_reason_out_of_set_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            snapshot(fail_reason=DecisionReasonV1.READY_ITEMS_AVAILABLE)

    def test_finalization_signal_mismatch_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            snapshot(final_outcome_kind=FinalOutcomeKindV1.COMPLETE)

    def test_ready_set_requires_ref(self) -> None:
        with self.assertRaises(ValidationError):
            snapshot(has_ready_items=True)

    def test_blocking_effect_requires_reconcile_at(self) -> None:
        with self.assertRaises(ValidationError):
            snapshot(has_blocking_unknown_effect=True, reconciliation_ready=True)

    def test_retryable_item_next_iteration_bounds(self) -> None:
        with self.assertRaises(ValidationError):
            RetryablePlanItemV1(
                plan_item_id=PLAN_ITEM_ID,
                next_iteration_no=4,
                retry_reason="NEW_CAUSAL_FACT",
            )

    def test_deadline_requires_utc(self) -> None:
        with self.assertRaises(ValidationError):
            snapshot(deadline_at=datetime(2026, 10, 8, 12, 0, 0))

    def test_directive_discriminator_roundtrip(self) -> None:
        payload = {
            "action": "FAIL_TASK",
            "fail_reason": DecisionReasonV1.SAFETY_FAIL_CLOSED,
        }
        directive = RUNTIME_DIRECTIVE_ADAPTER.validate_python(payload)
        self.assertIsInstance(directive, FailTaskDirectiveV1)
        self.assertEqual(
            RuntimeDirectiveActionV1(directive.action), RuntimeDirectiveActionV1.FAIL_TASK
        )

    def test_decision_ref_and_receipt_contracts(self) -> None:
        decision = evaluate_runtime_decision(snapshot(wake_sources=(wake_source(),)), 3)
        ref = RuntimeDecisionRefV1(
            decision_id=decision.decision_id,
            decision_sequence=decision.decision_sequence,
            action=RuntimeDirectiveActionV1.WAIT_EVENT,
            decision_digest=decision.decision_digest,
            expected_task_row_version=1,
        )
        receipt = RuntimeDecisionReceiptV1(
            decision_id=decision.decision_id,
            outcome=RuntimeDecisionReceiptOutcomeV1.APPLIED,
            decision_sequence=decision.decision_sequence,
            receipt_digest=ZERO_SHA,
        )
        self.assertEqual(ref.action, RuntimeDirectiveActionV1.WAIT_EVENT)
        self.assertEqual(receipt.outcome, RuntimeDecisionReceiptOutcomeV1.APPLIED)


if __name__ == "__main__":
    unittest.main()
