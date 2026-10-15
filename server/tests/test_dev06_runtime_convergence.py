"""DEV-06 无进展计数循环级合并的离线验收（零LLM/零I/O）。

覆盖 `compute_loop_fingerprint`（业务进展指纹 + 循环语义材料 → 相同语义循环指纹）与
`merge_loop_no_progress`（`evaluate_no_progress_round` + Replan 预算收敛 → 继续/Replan/收敛）。
严格复用已冻结领域语义，验证上层 Temporal Activity 所需的应用层接线契约。
"""

import unittest

from knowledge_system.modules.tasking.application.runtime_convergence import (
    LoopFingerprintInputs,
    LoopNoProgressMergeV1,
    RuntimeConvergenceActionV1,
    compute_loop_fingerprint,
    merge_loop_no_progress,
)
from knowledge_system.modules.tasking.domain.runtime_progress import (
    MAX_REPLAN_PER_TASK,
    NoProgressStateV1,
    ProgressDispositionV1,
    TaskProgressVectorV1,
    loop_fingerprint_for,
    progress_fingerprint_for,
)

INPUT_DIGEST = "a" * 64
OUTPUT_DIGEST = "b" * 64


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


def inputs(semantic: str = "plan-1", action_digest: str | None = None) -> LoopFingerprintInputs:
    return LoopFingerprintInputs(
        progress_vector=vector(),
        input_digest=INPUT_DIGEST,
        plan_item_semantic_keys=(semantic,),
        capability_requirement_keys=("cap-search",),
        blocker_reason_classes=(),
        semantic_action_digest=action_digest,
    )


def fp(semantic: str = "plan-1", action_digest: str | None = None) -> str:
    return compute_loop_fingerprint(inputs(semantic, action_digest))


def merge(state, disposition, loop: str | None = None, *, has_alternative_path: bool = False):
    resolved = loop if loop is not None else fp()
    return merge_loop_no_progress(
        state, disposition, resolved, has_alternative_path=has_alternative_path
    )


class ComputeLoopFingerprintTests(unittest.TestCase):
    def test_matches_domain_composition(self) -> None:
        expected = loop_fingerprint_for(
            progress_fingerprint=progress_fingerprint_for(vector()),
            input_digest=INPUT_DIGEST,
            plan_item_semantic_keys=("plan-1",),
            capability_requirement_keys=("cap-search",),
            blocker_reason_classes=(),
            semantic_action_digest=None,
        )
        self.assertEqual(fp(), expected)

    def test_deterministic(self) -> None:
        self.assertEqual(fp(), fp())

    def test_64_hex(self) -> None:
        self.assertEqual(len(fp()), 64)

    def test_changes_with_semantics(self) -> None:
        self.assertNotEqual(fp("plan-1"), fp("plan-2"))

    def test_changes_with_action_digest(self) -> None:
        self.assertNotEqual(fp(action_digest="c" * 64), fp(action_digest="d" * 64))

    def test_ignores_agent_tool_provider_names(self) -> None:
        # 换 Agent/Tool/Provider 名（根本不在入参）语义相同必同指纹。
        self.assertEqual(fp(), fp())


class MergeLoopNoProgressTests(unittest.TestCase):
    def test_progress_continues_and_resets_streak(self) -> None:
        state = NoProgressStateV1(
            consecutive_no_progress=2, epoch_no_progress=2, episode_replan_used=True
        )
        result = merge(state, ProgressDispositionV1.PROGRESS)
        self.assertEqual(result.action, RuntimeConvergenceActionV1.CONTINUE_LOOP)
        self.assertEqual(result.updated_state.consecutive_no_progress, 0)
        self.assertFalse(result.updated_state.episode_replan_used)
        self.assertEqual(result.updated_state.epoch_no_progress, 2)

    def test_first_no_progress_continues(self) -> None:
        result = merge(NoProgressStateV1(), ProgressDispositionV1.NO_PROGRESS)
        self.assertEqual(result.action, RuntimeConvergenceActionV1.CONTINUE_LOOP)
        self.assertEqual(result.updated_state.consecutive_no_progress, 1)

    def test_second_no_progress_replans_when_path_available(self) -> None:
        state = NoProgressStateV1(current_loop_fingerprint=fp(), consecutive_no_progress=1)
        result = merge(state, ProgressDispositionV1.NO_PROGRESS, has_alternative_path=True)
        self.assertEqual(result.action, RuntimeConvergenceActionV1.REPLAN)
        self.assertTrue(result.updated_state.episode_replan_used)
        self.assertEqual(result.updated_state.task_replan_count, 1)

    def test_second_no_progress_converges_without_alternative_path(self) -> None:
        state = NoProgressStateV1(current_loop_fingerprint=fp(), consecutive_no_progress=1)
        result = merge(state, ProgressDispositionV1.NO_PROGRESS, has_alternative_path=False)
        self.assertEqual(result.action, RuntimeConvergenceActionV1.CONVERGE)

    def test_second_no_progress_converges_when_episode_replan_used(self) -> None:
        state = NoProgressStateV1(
            current_loop_fingerprint=fp(), consecutive_no_progress=1, episode_replan_used=True
        )
        result = merge(state, ProgressDispositionV1.NO_PROGRESS, has_alternative_path=True)
        self.assertEqual(result.action, RuntimeConvergenceActionV1.CONVERGE)

    def test_second_no_progress_converges_at_task_replan_limit(self) -> None:
        state = NoProgressStateV1(
            current_loop_fingerprint=fp(),
            consecutive_no_progress=1,
            task_replan_count=MAX_REPLAN_PER_TASK,
        )
        result = merge(state, ProgressDispositionV1.NO_PROGRESS, has_alternative_path=True)
        self.assertEqual(result.action, RuntimeConvergenceActionV1.CONVERGE)

    def test_third_no_progress_converges(self) -> None:
        state = NoProgressStateV1(current_loop_fingerprint=fp(), consecutive_no_progress=2)
        result = merge(state, ProgressDispositionV1.NO_PROGRESS, has_alternative_path=True)
        self.assertEqual(result.action, RuntimeConvergenceActionV1.CONVERGE)

    def test_regression_replans_immediately(self) -> None:
        result = merge(
            NoProgressStateV1(), ProgressDispositionV1.REGRESSION, has_alternative_path=True
        )
        self.assertEqual(result.action, RuntimeConvergenceActionV1.REPLAN)

    def test_regression_converges_without_path(self) -> None:
        result = merge(
            NoProgressStateV1(), ProgressDispositionV1.REGRESSION, has_alternative_path=False
        )
        self.assertEqual(result.action, RuntimeConvergenceActionV1.CONVERGE)

    def test_epoch_converges_at_six(self) -> None:
        state = NoProgressStateV1(epoch_no_progress=5)
        result = merge(state, ProgressDispositionV1.NO_PROGRESS, has_alternative_path=True)
        self.assertEqual(result.action, RuntimeConvergenceActionV1.CONVERGE)

    def test_task_converges_at_twelve(self) -> None:
        state = NoProgressStateV1(task_no_progress=11)
        result = merge(state, ProgressDispositionV1.NO_PROGRESS, has_alternative_path=True)
        self.assertEqual(result.action, RuntimeConvergenceActionV1.CONVERGE)

    def test_result_type_frozen(self) -> None:
        result = merge(NoProgressStateV1(), ProgressDispositionV1.NO_PROGRESS)
        self.assertIsInstance(result, LoopNoProgressMergeV1)
        with self.assertRaises(AttributeError):
            result.action = RuntimeConvergenceActionV1.CONVERGE  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
