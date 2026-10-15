"""DEV-06 无进展计数器的循环级合并（零 LLM、零 I/O）。

把已冻结的进度领域规则接线为「循环级合并」契约，供上层 Temporal Activity 在两次
`RuntimeLoopService.advance` 之间推进无进展状态：先由 `compute_loop_fingerprint` 从
`TaskProgressVectorV1` 与循环语义材料派生出相同语义循环指纹（换 Agent/Tool/Provider 名不改指纹），
再由 `merge_loop_no_progress` 把一次 Action Run 的 `ProgressDispositionV1` 喂给
`evaluate_no_progress_round`，并把 `REPLAN_ELIGIBLE` 结合 Replan 预算（`replan_permitted` /
`mark_replan_used`）收敛为「继续 / Replan / 强制收敛」三态信号。

`CONTINUE_LOOP` / `REPLAN` / `CONVERGE` 如何编码进下一个 `RuntimeDecisionSnapshotV1` 的
`replan_admission` / `finalization_reason` / `fail_reason` 属 `RuntimeDecisionContextBuilder`
的职责，本切片只产出收敛动作与更新后的 `NoProgressStateV1`，不越界投影决策快照。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from knowledge_system.modules.tasking.domain.core_types import Sha256Hex, StableKey
from knowledge_system.modules.tasking.domain.runtime_progress import (
    NoProgressActionV1,
    NoProgressStateV1,
    ProgressDispositionV1,
    TaskProgressVectorV1,
    evaluate_no_progress_round,
    loop_fingerprint_for,
    mark_replan_used,
    progress_fingerprint_for,
    replan_permitted,
)


@dataclass(frozen=True, slots=True)
class LoopFingerprintInputs:
    """循环指纹材料；`progress_vector` 由后续 `RuntimeDecisionContextBuilder` 产出。"""

    progress_vector: TaskProgressVectorV1
    input_digest: Sha256Hex
    plan_item_semantic_keys: tuple[StableKey, ...]
    capability_requirement_keys: tuple[StableKey, ...]
    blocker_reason_classes: tuple[StableKey, ...]
    semantic_action_digest: Sha256Hex | None = None


def compute_loop_fingerprint(inputs: LoopFingerprintInputs) -> Sha256Hex:
    """从进展向量与循环语义材料派生相同语义循环指纹（复用已冻结领域函数）。"""

    return loop_fingerprint_for(
        progress_fingerprint_for(inputs.progress_vector),
        inputs.input_digest,
        inputs.plan_item_semantic_keys,
        inputs.capability_requirement_keys,
        inputs.blocker_reason_classes,
        inputs.semantic_action_digest,
    )


class RuntimeConvergenceActionV1(StrEnum):
    """循环级收敛动作：继续 / Replan（消费一次预算）/ 强制收敛。"""

    CONTINUE_LOOP = "CONTINUE_LOOP"
    REPLAN = "REPLAN"
    CONVERGE = "CONVERGE"


@dataclass(frozen=True, slots=True)
class LoopNoProgressMergeV1:
    action: RuntimeConvergenceActionV1
    updated_state: NoProgressStateV1


def merge_loop_no_progress(
    state: NoProgressStateV1,
    disposition: ProgressDispositionV1,
    loop_fingerprint: Sha256Hex,
    *,
    has_alternative_path: bool,
) -> LoopNoProgressMergeV1:
    """把一次 Action Run 的无进展事实合并进循环，收敛为三态动作。

    - `CONTINUE_LOOP`：`evaluate_no_progress_round` 判 `CONTINUE`（PROGRESS 归零 streak，或
      NO_PROGRESS 未达 `REPLAN_ELIGIBLE_AT`）。
    - `REPLAN`：`REPLAN_ELIGIBLE` 且 `replan_permitted`（确有替代路径、本 Episode 未消费、
      Task 级 ≤ `MAX_REPLAN_PER_TASK`），`mark_replan_used` 消费一次。
    - `CONVERGE`：阈值强制收敛，或 `REPLAN_ELIGIBLE` 但 Replan 预算耗尽。
    """

    round_result = evaluate_no_progress_round(state, disposition, loop_fingerprint)
    updated = round_result.updated_state
    if round_result.action is NoProgressActionV1.CONTINUE:
        return LoopNoProgressMergeV1(RuntimeConvergenceActionV1.CONTINUE_LOOP, updated)
    if round_result.action is NoProgressActionV1.REPLAN_ELIGIBLE and replan_permitted(
        updated, has_alternative_path
    ):
        return LoopNoProgressMergeV1(RuntimeConvergenceActionV1.REPLAN, mark_replan_used(updated))
    return LoopNoProgressMergeV1(RuntimeConvergenceActionV1.CONVERGE, updated)


__all__ = [
    "LoopFingerprintInputs",
    "LoopNoProgressMergeV1",
    "RuntimeConvergenceActionV1",
    "compute_loop_fingerprint",
    "merge_loop_no_progress",
]
