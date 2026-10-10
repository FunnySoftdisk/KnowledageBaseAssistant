"""DEV-06：进展向量、循环指纹与无进展强制收敛离线验收（零LLM/零I/O）。"""

import unittest
from uuid import UUID

from pydantic import ValidationError

from knowledge_system.modules.tasking.domain.core_types import VersionedRefV1
from knowledge_system.modules.tasking.domain.runtime_progress import (
    MAX_REPLAN_PER_TASK,
    NoProgressActionV1,
    NoProgressStateV1,
    ProgressDeltaKindV1,
    ProgressDispositionV1,
    RuntimeProgressSnapshotV1,
    TaskProgressVectorV1,
    classify_disposition,
    evaluate_no_progress_round,
    freeze_runtime_progress_snapshot,
    is_probe_eligible,
    loop_fingerprint_for,
    mark_replan_used,
    new_input_epoch_reset,
    progress_fingerprint_for,
    progress_snapshot_digest_for,
    replan_permitted,
)

ZERO_SHA = "0" * 64
INPUT_DIGEST = "a" * 64
OUTPUT_DIGEST = "b" * 64
REF_A_ID = UUID("11111111-1111-4111-8111-111111111111")
REF_B_ID = UUID("22222222-2222-4222-8222-222222222222")


def ref(ref_id: UUID, version: int = 1) -> VersionedRefV1:
    return VersionedRefV1(ref_id=ref_id, version=version, integrity_digest=ZERO_SHA)


def vector(**overrides) -> TaskProgressVectorV1:
    base = dict(
        input_digest=INPUT_DIGEST,
        output_contract_digest=OUTPUT_DIGEST,
        satisfied_criterion_keys=(),
        adopted_evidence_refs=(),
        merged_result_refs=(),
        closed_required_gap_keys=(),
        resolved_required_conflict_keys=(),
        resolved_required_subgoal_keys=(),
        confirmed_effect_refs=(),
        user_decision_refs=(),
        permanent_blocker_classes=(),
    )
    base.update(overrides)
    return TaskProgressVectorV1.model_validate(base)


def loop_fp(semantic: str = "plan-1", action_digest: str | None = None) -> str:
    return loop_fingerprint_for(
        progress_fingerprint=progress_fingerprint_for(vector()),
        input_digest=INPUT_DIGEST,
        plan_item_semantic_keys=(semantic,),
        capability_requirement_keys=("cap-search",),
        blocker_reason_classes=(),
        semantic_action_digest=action_digest,
    )


def snapshot(**overrides) -> RuntimeProgressSnapshotV1:
    base = dict(
        contract_version="runtime_progress_snapshot_v1",
        epoch=0,
        predecessor_digest=None,
        event_high_watermark=0,
        progress_fingerprint=progress_fingerprint_for(vector()),
        loop_fingerprint=loop_fp(),
        disposition=ProgressDispositionV1.NO_PROGRESS,
        delta_kinds=(),
        delta_refs=(),
        regressed_refs=(),
    )
    base.update(overrides)
    return RuntimeProgressSnapshotV1.model_validate(base)


def round_result(state, disposition, loop: str):
    return evaluate_no_progress_round(state, disposition, loop)


class DeltaWhitelistTests(unittest.TestCase):
    def test_eight_delta_kinds(self) -> None:
        self.assertEqual(len(list(ProgressDeltaKindV1)), 8)


class ClassifyDispositionTests(unittest.TestCase):
    def test_progress_requires_delta(self) -> None:
        self.assertEqual(
            classify_disposition((ProgressDeltaKindV1.CRITERION_SATISFIED,), ()),
            ProgressDispositionV1.PROGRESS,
        )

    def test_no_progress_requires_empty(self) -> None:
        self.assertEqual(classify_disposition((), ()), ProgressDispositionV1.NO_PROGRESS)

    def test_regression_requires_regressed_refs(self) -> None:
        self.assertEqual(
            classify_disposition((), (ref(REF_A_ID),)), ProgressDispositionV1.REGRESSION
        )

    def test_delta_and_regression_conflict_rejected(self) -> None:
        with self.assertRaises(ValueError):
            classify_disposition((ProgressDeltaKindV1.NEW_ADOPTED_INFORMATION,), (ref(REF_A_ID),))


class ProgressFingerprintTests(unittest.TestCase):
    def test_fingerprint_deterministic(self) -> None:
        self.assertEqual(progress_fingerprint_for(vector()), progress_fingerprint_for(vector()))

    def test_fingerprint_64_hex(self) -> None:
        self.assertEqual(len(progress_fingerprint_for(vector())), 64)

    def test_fingerprint_order_independent(self) -> None:
        first = vector(adopted_evidence_refs=(ref(REF_A_ID), ref(REF_B_ID)))
        second = vector(adopted_evidence_refs=(ref(REF_B_ID), ref(REF_A_ID)))
        self.assertEqual(progress_fingerprint_for(first), progress_fingerprint_for(second))

    def test_fingerprint_changes_with_content(self) -> None:
        first = progress_fingerprint_for(vector())
        second = progress_fingerprint_for(vector(satisfied_criterion_keys=("criterion-1",)))
        self.assertNotEqual(first, second)


class LoopFingerprintTests(unittest.TestCase):
    def test_loop_fingerprint_deterministic(self) -> None:
        self.assertEqual(loop_fp(), loop_fp())

    def test_loop_fingerprint_order_independent(self) -> None:
        first = loop_fingerprint_for(
            progress_fingerprint=progress_fingerprint_for(vector()),
            input_digest=INPUT_DIGEST,
            plan_item_semantic_keys=("a", "b"),
            capability_requirement_keys=("x", "y"),
            blocker_reason_classes=(),
        )
        second = loop_fingerprint_for(
            progress_fingerprint=progress_fingerprint_for(vector()),
            input_digest=INPUT_DIGEST,
            plan_item_semantic_keys=("b", "a"),
            capability_requirement_keys=("y", "x"),
            blocker_reason_classes=(),
        )
        self.assertEqual(first, second)

    def test_loop_fingerprint_changes_with_semantics(self) -> None:
        self.assertNotEqual(loop_fp("plan-1"), loop_fp("plan-2"))

    def test_loop_fingerprint_changes_with_action_digest(self) -> None:
        self.assertNotEqual(loop_fp(action_digest="c" * 64), loop_fp(action_digest="d" * 64))

    def test_loop_fingerprint_ignores_agent_tool_provider_names(self) -> None:
        # Agent/Tool/Provider 名称根本不在入参中；语义相同必同指纹（换Agent不换语义=同一循环）。
        self.assertEqual(
            loop_fingerprint_for(
                progress_fingerprint=progress_fingerprint_for(vector()),
                input_digest=INPUT_DIGEST,
                plan_item_semantic_keys=("search-internal-evidence",),
                capability_requirement_keys=("read-only-retrieval",),
                blocker_reason_classes=(),
            ),
            loop_fingerprint_for(
                progress_fingerprint=progress_fingerprint_for(vector()),
                input_digest=INPUT_DIGEST,
                plan_item_semantic_keys=("search-internal-evidence",),
                capability_requirement_keys=("read-only-retrieval",),
                blocker_reason_classes=(),
            ),
        )


class SnapshotTests(unittest.TestCase):
    def test_snapshot_digest_stable_64_hex(self) -> None:
        digest = progress_snapshot_digest_for(snapshot())
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, progress_snapshot_digest_for(snapshot()))

    def test_freeze_keeps_identity(self) -> None:
        frozen = freeze_runtime_progress_snapshot(snapshot())
        self.assertEqual(frozen.epoch, 0)
        self.assertEqual(len(frozen.snapshot_digest), 64)

    def test_disposition_mismatch_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            snapshot(disposition=ProgressDispositionV1.PROGRESS, delta_kinds=())

    def test_regression_requires_regressed_refs(self) -> None:
        with self.assertRaises(ValidationError):
            snapshot(disposition=ProgressDispositionV1.REGRESSION, regressed_refs=())

    def test_epoch_non_negative(self) -> None:
        with self.assertRaises(ValidationError):
            snapshot(epoch=-1)

    def test_extra_field_forbidden(self) -> None:
        with self.assertRaises(ValidationError):
            RuntimeProgressSnapshotV1.model_validate({**snapshot().model_dump(), "forged": "value"})


class NoProgressRoundTests(unittest.TestCase):
    def test_progress_resets_episode_counters_keeps_epoch_task(self) -> None:
        state = NoProgressStateV1(
            consecutive_no_progress=2,
            epoch_no_progress=2,
            task_no_progress=5,
            episode_replan_used=True,
        )
        result = round_result(state, ProgressDispositionV1.PROGRESS, loop_fp())
        self.assertEqual(result.action, NoProgressActionV1.CONTINUE)
        self.assertEqual(result.updated_state.consecutive_no_progress, 0)
        self.assertFalse(result.updated_state.episode_replan_used)
        self.assertEqual(result.updated_state.epoch_no_progress, 2)
        self.assertEqual(result.updated_state.task_no_progress, 5)

    def test_first_no_progress_continues(self) -> None:
        result = round_result(NoProgressStateV1(), ProgressDispositionV1.NO_PROGRESS, loop_fp())
        self.assertEqual(result.action, NoProgressActionV1.CONTINUE)
        self.assertEqual(result.updated_state.consecutive_no_progress, 1)

    def test_second_no_progress_replan_eligible(self) -> None:
        state = NoProgressStateV1(current_loop_fingerprint=loop_fp(), consecutive_no_progress=1)
        result = round_result(state, ProgressDispositionV1.NO_PROGRESS, loop_fp())
        self.assertEqual(result.action, NoProgressActionV1.REPLAN_ELIGIBLE)

    def test_third_no_progress_converges(self) -> None:
        state = NoProgressStateV1(current_loop_fingerprint=loop_fp(), consecutive_no_progress=2)
        result = round_result(state, ProgressDispositionV1.NO_PROGRESS, loop_fp())
        self.assertEqual(result.action, NoProgressActionV1.CONVERGE)

    def test_epoch_converges_at_six(self) -> None:
        state = NoProgressStateV1(epoch_no_progress=5)
        result = round_result(state, ProgressDispositionV1.NO_PROGRESS, loop_fp())
        self.assertEqual(result.action, NoProgressActionV1.CONVERGE)

    def test_task_converges_at_twelve(self) -> None:
        state = NoProgressStateV1(task_no_progress=11)
        result = round_result(state, ProgressDispositionV1.NO_PROGRESS, loop_fp())
        self.assertEqual(result.action, NoProgressActionV1.CONVERGE)

    def test_regression_replan_eligible_immediately(self) -> None:
        result = round_result(NoProgressStateV1(), ProgressDispositionV1.REGRESSION, loop_fp())
        self.assertEqual(result.action, NoProgressActionV1.REPLAN_ELIGIBLE)

    def test_same_loop_tracking(self) -> None:
        state = NoProgressStateV1(
            current_loop_fingerprint=loop_fp(),
            same_loop_no_progress=1,
            consecutive_no_progress=1,
        )
        result = round_result(state, ProgressDispositionV1.NO_PROGRESS, loop_fp())
        self.assertEqual(result.updated_state.same_loop_no_progress, 2)

    def test_new_loop_resets_same_loop_not_consecutive(self) -> None:
        state = NoProgressStateV1(
            current_loop_fingerprint=loop_fp("plan-1"), consecutive_no_progress=1
        )
        result = round_result(state, ProgressDispositionV1.NO_PROGRESS, loop_fp("plan-2"))
        self.assertEqual(result.updated_state.same_loop_no_progress, 1)
        self.assertEqual(result.updated_state.consecutive_no_progress, 2)
        self.assertEqual(result.action, NoProgressActionV1.REPLAN_ELIGIBLE)


class ReplanPermittedTests(unittest.TestCase):
    def test_replan_requires_alternative_path(self) -> None:
        self.assertFalse(replan_permitted(NoProgressStateV1(), has_alternative_path=False))

    def test_replan_permitted_when_path_and_unused(self) -> None:
        self.assertTrue(replan_permitted(NoProgressStateV1(), has_alternative_path=True))

    def test_replan_blocked_when_episode_used(self) -> None:
        state = NoProgressStateV1(episode_replan_used=True)
        self.assertFalse(replan_permitted(state, has_alternative_path=True))

    def test_replan_blocked_at_task_limit(self) -> None:
        state = NoProgressStateV1(task_replan_count=MAX_REPLAN_PER_TASK)
        self.assertFalse(replan_permitted(state, has_alternative_path=True))

    def test_mark_replan_used_updates_state(self) -> None:
        updated = mark_replan_used(NoProgressStateV1())
        self.assertTrue(updated.episode_replan_used)
        self.assertEqual(updated.task_replan_count, 1)


class ProbeAndEpochTests(unittest.TestCase):
    def test_probe_eligible_for_unseen_loop(self) -> None:
        state = NoProgressStateV1(episode_seen_loop_fingerprints=(loop_fp("seen"),))
        self.assertTrue(is_probe_eligible(state, loop_fp("unseen")))

    def test_probe_not_eligible_for_seen_loop(self) -> None:
        state = NoProgressStateV1(episode_seen_loop_fingerprints=(loop_fp("seen"),))
        self.assertFalse(is_probe_eligible(state, loop_fp("seen")))

    def test_new_input_resets_epoch_keeps_task(self) -> None:
        state = NoProgressStateV1(epoch_no_progress=4, task_no_progress=7)
        updated = new_input_epoch_reset(state)
        self.assertEqual(updated.epoch_no_progress, 0)
        self.assertEqual(updated.task_no_progress, 7)


if __name__ == "__main__":
    unittest.main()
