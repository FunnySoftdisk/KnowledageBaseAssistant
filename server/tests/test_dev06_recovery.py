"""DEV-06 Final 离线验收：§5.35 子状态机/失败取消恢复 + §5.36 Ready 门禁。

不接真实模型/PG/Temporal；只证明 Finalization 子状态机只前进与终态吸收、十终态/两控制错误码、
Snapshot 重建 ≤3、Deadline 超期、模型调用恢复分类、取消对提交线性化，以及 Ready 门禁检查清单的
确定性判定（READY/NOT_READY + 缺失项 + JCS 摘要）。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from knowledge_system.modules.tasking.domain.final_episode import (
    FINAL_CONTROL_RESULT_CODES,
    FINAL_CONTROL_TRANSACTION_ATTEMPTS,
    FINAL_MODEL_REQUEST_ATTEMPTS,
    FINAL_SNAPSHOT_REBUILD_LIMIT,
    FINAL_TERMINAL_ERROR_CODES,
    FINAL_TERMINAL_SUBSTATES,
    FinalErrorCodeV1,
    FinalizationSubstateV1,
    FinalModelCallResumptionV1,
    FinalModelCallStateV1,
    advance_substate,
    cancel_vs_commit_terminal,
    classify_model_call_resumption,
    deadline_verdict,
    final_commit_block,
    is_terminal_error_code,
    is_terminal_substate,
    snapshot_rebuild_verdict,
)
from knowledge_system.modules.tasking.domain.final_readiness import (
    FINAL_READY_REQUIRED_ITEMS,
    compute_final_readiness,
)


class FinalizationEpisodeTests(unittest.TestCase):
    """§5.35 失败、取消与恢复的确定性规则。"""

    def test_forward_linear_sequence(self):
        expected = [
            "SNAPSHOT_BOUND",
            "MODEL_RUNNING",
            "CANDIDATE_STAGED",
            "VALIDATING",
            "REPAIRING",
            "ACCEPTED",
            "ASSEMBLING",
            "COMMITTING",
            "COMMITTED",
        ]
        current = FinalizationSubstateV1.CREATED
        for name in expected:
            current = advance_substate(current, FinalizationSubstateV1(name))
            self.assertEqual(current, FinalizationSubstateV1(name))

    def test_advance_forward_jump_allowed(self):
        self.assertEqual(
            advance_substate(FinalizationSubstateV1.CREATED, FinalizationSubstateV1.VALIDATING),
            FinalizationSubstateV1.VALIDATING,
        )

    def test_advance_backward_rejected(self):
        with self.assertRaisesRegex(ValueError, "FINALIZATION_SUBSTATE_BACKWARD"):
            advance_substate(
                FinalizationSubstateV1.VALIDATING,
                FinalizationSubstateV1.CANDIDATE_STAGED,
            )

    def test_advance_same_substate_rejected(self):
        with self.assertRaisesRegex(ValueError, "FINALIZATION_SUBSTATE_BACKWARD"):
            advance_substate(
                FinalizationSubstateV1.CANDIDATE_STAGED,
                FinalizationSubstateV1.CANDIDATE_STAGED,
            )

    def test_advance_to_terminal_from_forward(self):
        self.assertEqual(
            advance_substate(FinalizationSubstateV1.ASSEMBLING, FinalizationSubstateV1.FAILED),
            FinalizationSubstateV1.FAILED,
        )

    def test_advance_from_terminal_rejected(self):
        with self.assertRaisesRegex(ValueError, "FINALIZATION_SUBSTATE_TERMINAL"):
            advance_substate(FinalizationSubstateV1.CANCELLED, FinalizationSubstateV1.COMMITTED)

    def test_terminal_substates_membership(self):
        self.assertEqual(
            FINAL_TERMINAL_SUBSTATES,
            frozenset(
                {
                    FinalizationSubstateV1.INVALIDATED_STALE,
                    FinalizationSubstateV1.FAILED,
                    FinalizationSubstateV1.CANCELLED,
                }
            ),
        )
        self.assertTrue(is_terminal_substate(FinalizationSubstateV1.FAILED))
        self.assertFalse(is_terminal_substate(FinalizationSubstateV1.COMMITTED))

    def test_error_code_taxonomy(self):
        self.assertEqual(len(FINAL_TERMINAL_ERROR_CODES), 10)
        self.assertEqual(len(FINAL_CONTROL_RESULT_CODES), 2)
        self.assertTrue(FINAL_TERMINAL_ERROR_CODES.isdisjoint(FINAL_CONTROL_RESULT_CODES))
        self.assertTrue(is_terminal_error_code(FinalErrorCodeV1.TASK_DEADLINE_EXCEEDED))
        self.assertFalse(is_terminal_error_code(FinalErrorCodeV1.FINAL_CANDIDATE_REPAIR_REQUIRED))
        self.assertTrue(is_terminal_error_code(FinalErrorCodeV1.TASK_RESULT_COMMIT_CONFLICT))

    def test_snapshot_rebuild_verdict(self):
        for count in (0, 1, 2):
            self.assertEqual(snapshot_rebuild_verdict(count), "ALLOWED")
        self.assertEqual(snapshot_rebuild_verdict(3), "FINAL_SYNTHESIS_INPUT_UNSTABLE")
        self.assertEqual(FINAL_SNAPSHOT_REBUILD_LIMIT, 3)

    def test_snapshot_rebuild_negative_rejected(self):
        with self.assertRaisesRegex(ValueError, "SNAPSHOT_REBUILD_COUNT_NEGATIVE"):
            snapshot_rebuild_verdict(-1)

    def test_deadline_verdict(self):
        deadline = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
        self.assertEqual(
            deadline_verdict(clock=deadline - timedelta(seconds=1), deadline_at=deadline),
            "WITHIN_DEADLINE",
        )
        self.assertEqual(
            deadline_verdict(clock=deadline, deadline_at=deadline),
            "TASK_DEADLINE_EXCEEDED",
        )
        self.assertEqual(
            deadline_verdict(clock=deadline + timedelta(seconds=1), deadline_at=deadline),
            "TASK_DEADLINE_EXCEEDED",
        )

    def test_deadline_verdict_naive_rejected(self):
        with self.assertRaisesRegex(ValueError, "UTC_DATETIME_REQUIRED"):
            deadline_verdict(clock=datetime(2026, 1, 1), deadline_at=datetime(2026, 1, 2))

    def test_model_call_resumption_classification(self):
        self.assertEqual(
            classify_model_call_resumption(FinalModelCallStateV1.COMPLETED),
            FinalModelCallResumptionV1.REUSE_COMPLETED,
        )
        self.assertEqual(
            classify_model_call_resumption(FinalModelCallStateV1.TRANSIENT_NO_DELTA),
            FinalModelCallResumptionV1.RETRY_SAME_BINDING,
        )
        self.assertEqual(
            classify_model_call_resumption(FinalModelCallStateV1.STREAMING_INTERRUPTED),
            FinalModelCallResumptionV1.RECONCILE_FIRST,
        )
        self.assertEqual(
            classify_model_call_resumption(FinalModelCallStateV1.UNKNOWN),
            FinalModelCallResumptionV1.RECONCILE_FIRST,
        )

    def test_cancel_vs_commit_terminal(self):
        self.assertEqual(cancel_vs_commit_terminal("CANCEL"), FinalizationSubstateV1.CANCELLED)
        self.assertEqual(cancel_vs_commit_terminal("COMMIT"), FinalizationSubstateV1.COMMITTED)

    def test_final_commit_block(self):
        self.assertIsNone(final_commit_block(deadline_exceeded=False, cancelled=False))
        self.assertEqual(
            final_commit_block(deadline_exceeded=True, cancelled=False),
            FinalizationSubstateV1.FAILED,
        )
        self.assertEqual(
            final_commit_block(deadline_exceeded=False, cancelled=True),
            FinalizationSubstateV1.CANCELLED,
        )
        # 取消优先于超期
        self.assertEqual(
            final_commit_block(deadline_exceeded=True, cancelled=True),
            FinalizationSubstateV1.CANCELLED,
        )

    def test_activity_attempt_constants(self):
        self.assertEqual(FINAL_MODEL_REQUEST_ATTEMPTS, 2)
        self.assertEqual(FINAL_CONTROL_TRANSACTION_ATTEMPTS, 5)


class FinalReadinessGateTests(unittest.TestCase):
    """§5.36 Ready 门禁的确定性判定。"""

    def _all_items(self) -> tuple[str, ...]:
        return tuple(sorted(FINAL_READY_REQUIRED_ITEMS))

    def test_all_present_ready(self):
        report = compute_final_readiness(self._all_items())
        self.assertEqual(report.verdict, "READY")
        self.assertEqual(report.missing_required_items, ())

    def test_none_present_not_ready(self):
        report = compute_final_readiness(())
        self.assertEqual(report.verdict, "NOT_READY")
        self.assertEqual(report.missing_required_items, self._all_items())

    def test_partial_missing_sorted(self):
        present = self._all_items()[:-3]
        report = compute_final_readiness(present)
        self.assertEqual(report.verdict, "NOT_READY")
        self.assertEqual(report.missing_required_items, self._all_items()[-3:])

    def test_unknown_item_rejected(self):
        with self.assertRaisesRegex(ValueError, "FINAL_READY_UNKNOWN_ITEM"):
            compute_final_readiness(("SCHEMA", "NOT_A_REAL_ITEM"))

    def test_digest_deterministic_and_sensitive(self):
        full = compute_final_readiness(self._all_items())
        full_again = compute_final_readiness(self._all_items())
        self.assertEqual(full.readiness_digest, full_again.readiness_digest)
        missing = compute_final_readiness(self._all_items()[:-1])
        self.assertNotEqual(full.readiness_digest, missing.readiness_digest)


if __name__ == "__main__":
    unittest.main()
