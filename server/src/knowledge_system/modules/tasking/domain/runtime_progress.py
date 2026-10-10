"""AGT-07 进展向量、循环指纹与无进展强制收敛（零LLM、零I/O）。

三个指纹语义严格分离：`decision_basis_digest`（决策幂等，见 runtime_decision）、
`task_progress_fingerprint`（业务进展）、`attempt_loop_fingerprint`（相同语义循环）。
无进展计数只按确定性阈值收敛，Controller 不信任 `AttemptAssessment.progress` 直接计数。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from .core_types import (
    Sha256Hex,
    StableKey,
    StrictContractV1,
    VersionedRefV1,
    canonical_json_sha256,
)

# 收敛阈值（冻结，见模块08 §5.25）。
MAX_SAME_LOOP_NO_PROGRESS = 2
REPLAN_ELIGIBLE_AT = 2
FORCE_CONVERGE_AT = 3
EPOCH_CONVERGE_AT = 6
TASK_CONVERGE_AT = 12
MAX_REPLAN_PER_EPISODE = 1
MAX_REPLAN_PER_TASK = 3


class ProgressDeltaKindV1(StrEnum):
    """有效进展 Delta 白名单；其余（NO_OP/改版/换Agent/日志/预算/自由Assessment等）不算进展。"""

    NEW_ADOPTED_INFORMATION = "NEW_ADOPTED_INFORMATION"
    CRITERION_SATISFIED = "CRITERION_SATISFIED"
    REQUIRED_GAP_CLOSED = "REQUIRED_GAP_CLOSED"
    REQUIRED_CONFLICT_RESOLVED = "REQUIRED_CONFLICT_RESOLVED"
    REQUIRED_SUBGOAL_RESOLVED = "REQUIRED_SUBGOAL_RESOLVED"
    EXTERNAL_EFFECT_CONFIRMED = "EXTERNAL_EFFECT_CONFIRMED"
    USER_INPUT_ADVANCED = "USER_INPUT_ADVANCED"
    NEW_PERMANENT_BLOCKER_CLASSIFIED = "NEW_PERMANENT_BLOCKER_CLASSIFIED"


class ProgressDispositionV1(StrEnum):
    PROGRESS = "PROGRESS"
    NO_PROGRESS = "NO_PROGRESS"
    REGRESSION = "REGRESSION"


class NoProgressActionV1(StrEnum):
    CONTINUE = "CONTINUE"
    REPLAN_ELIGIBLE = "REPLAN_ELIGIBLE"
    CONVERGE = "CONVERGE"


class TaskProgressVectorV1(StrictContractV1):
    """业务进展向量；只含可确定性判定的维度，ID/时间/预算/重试次数/模型Assessment不进入。"""

    input_digest: Sha256Hex
    output_contract_digest: Sha256Hex
    satisfied_criterion_keys: tuple[StableKey, ...] = Field(max_length=128)
    adopted_evidence_refs: tuple[VersionedRefV1, ...] = Field(max_length=64)
    merged_result_refs: tuple[VersionedRefV1, ...] = Field(max_length=256)
    closed_required_gap_keys: tuple[StableKey, ...] = Field(max_length=128)
    resolved_required_conflict_keys: tuple[StableKey, ...] = Field(max_length=64)
    resolved_required_subgoal_keys: tuple[StableKey, ...] = Field(max_length=64)
    confirmed_effect_refs: tuple[VersionedRefV1, ...] = Field(max_length=64)
    user_decision_refs: tuple[VersionedRefV1, ...] = Field(max_length=64)
    permanent_blocker_classes: tuple[StableKey, ...] = Field(max_length=16)


def _sorted_ref_keys(refs: tuple[VersionedRefV1, ...]) -> list[list[str | int]]:
    return sorted([[str(ref.ref_id).lower(), ref.version, ref.integrity_digest] for ref in refs])


def progress_fingerprint_for(vector: TaskProgressVectorV1) -> str:
    """稳定排序后以 RFC 8785 JCS+SHA-256 生成业务进展指纹。"""

    payload = {
        "input_digest": vector.input_digest,
        "output_contract_digest": vector.output_contract_digest,
        "satisfied_criterion_keys": sorted(vector.satisfied_criterion_keys),
        "adopted_evidence_refs": _sorted_ref_keys(vector.adopted_evidence_refs),
        "merged_result_refs": _sorted_ref_keys(vector.merged_result_refs),
        "closed_required_gap_keys": sorted(vector.closed_required_gap_keys),
        "resolved_required_conflict_keys": sorted(vector.resolved_required_conflict_keys),
        "resolved_required_subgoal_keys": sorted(vector.resolved_required_subgoal_keys),
        "confirmed_effect_refs": _sorted_ref_keys(vector.confirmed_effect_refs),
        "user_decision_refs": _sorted_ref_keys(vector.user_decision_refs),
        "permanent_blocker_classes": sorted(vector.permanent_blocker_classes),
    }
    return canonical_json_sha256(payload)


def loop_fingerprint_for(
    progress_fingerprint: Sha256Hex,
    input_digest: Sha256Hex,
    plan_item_semantic_keys: tuple[StableKey, ...],
    capability_requirement_keys: tuple[StableKey, ...],
    blocker_reason_classes: tuple[StableKey, ...],
    semantic_action_digest: Sha256Hex | None = None,
) -> str:
    """相同语义循环指纹；排除 Tool/Provider/Agent 名称与 Iteration/随机 ID。

    换 Agent、Tool 或 Provider 但语义相同仍派生同一循环指纹（这些名称根本不在入参中）。
    """

    payload = {
        "progress_fingerprint": progress_fingerprint,
        "input_digest": input_digest,
        "plan_item_semantics": sorted(plan_item_semantic_keys),
        "capability_requirements": sorted(capability_requirement_keys),
        "blocker_reason_classes": sorted(blocker_reason_classes),
        "semantic_action_digest": semantic_action_digest,
    }
    return canonical_json_sha256(payload)


def classify_disposition(
    delta_kinds: tuple[ProgressDeltaKindV1, ...],
    regressed_refs: tuple[VersionedRefV1, ...],
) -> ProgressDispositionV1:
    """PROGRESS（有白名单Delta）/ REGRESSION（结果失效或Criterion倒退）/ NO_PROGRESS。"""

    has_delta = bool(delta_kinds)
    has_regression = bool(regressed_refs)
    if has_delta and has_regression:
        raise ValueError("RUNTIME_PROGRESS_DELTA_REGRESSION_CONFLICT")
    if has_regression:
        return ProgressDispositionV1.REGRESSION
    if has_delta:
        return ProgressDispositionV1.PROGRESS
    return ProgressDispositionV1.NO_PROGRESS


class RuntimeProgressSnapshotV1(StrictContractV1):
    """ACTION Branch/Wave 屏障准入/Merge 后写的不可变进展快照（内容寻址，无UUID Namespace）。"""

    contract_version: Literal["runtime_progress_snapshot_v1"]
    epoch: int = Field(ge=0)
    predecessor_digest: Sha256Hex | None = None
    event_high_watermark: int = Field(ge=0)
    progress_fingerprint: Sha256Hex
    loop_fingerprint: Sha256Hex
    disposition: ProgressDispositionV1
    delta_kinds: tuple[ProgressDeltaKindV1, ...] = Field(max_length=8)
    delta_refs: tuple[VersionedRefV1, ...] = Field(max_length=16)
    regressed_refs: tuple[VersionedRefV1, ...] = Field(max_length=16)
    snapshot_digest: Sha256Hex = "0" * 64

    @model_validator(mode="after")
    def validate_disposition(self) -> RuntimeProgressSnapshotV1:
        expected = classify_disposition(self.delta_kinds, self.regressed_refs)
        if self.disposition != expected:
            raise ValueError("RUNTIME_PROGRESS_DISPOSITION_MISMATCH")
        return self


def progress_snapshot_digest_for(snapshot: RuntimeProgressSnapshotV1) -> str:
    return canonical_json_sha256(snapshot.model_dump(mode="json", exclude={"snapshot_digest"}))


def freeze_runtime_progress_snapshot(
    snapshot: RuntimeProgressSnapshotV1,
) -> RuntimeProgressSnapshotV1:
    return snapshot.model_copy(update={"snapshot_digest": progress_snapshot_digest_for(snapshot)})


class NoProgressStateV1(StrictContractV1):
    """无进展计数器；新Input只清Epoch连续值、不清Task生命周期累计。"""

    current_loop_fingerprint: Sha256Hex | None = None
    same_loop_no_progress: int = Field(default=0, ge=0)
    consecutive_no_progress: int = Field(default=0, ge=0)
    epoch_no_progress: int = Field(default=0, ge=0)
    task_no_progress: int = Field(default=0, ge=0)
    episode_replan_used: bool = False
    task_replan_count: int = Field(default=0, ge=0)
    episode_seen_loop_fingerprints: tuple[Sha256Hex, ...] = Field(default=(), max_length=32)


class NoProgressRoundResultV1(StrictContractV1):
    action: NoProgressActionV1
    updated_state: NoProgressStateV1


def _converge_action(
    consecutive: int, epoch: int, task: int, disposition: ProgressDispositionV1
) -> NoProgressActionV1:
    if consecutive >= FORCE_CONVERGE_AT or epoch >= EPOCH_CONVERGE_AT or task >= TASK_CONVERGE_AT:
        return NoProgressActionV1.CONVERGE
    if disposition == ProgressDispositionV1.REGRESSION or consecutive >= REPLAN_ELIGIBLE_AT:
        return NoProgressActionV1.REPLAN_ELIGIBLE
    return NoProgressActionV1.CONTINUE


def evaluate_no_progress_round(
    state: NoProgressStateV1,
    disposition: ProgressDispositionV1,
    loop_fingerprint: Sha256Hex,
) -> NoProgressRoundResultV1:
    """按冻结阈值推进无进展计数并给出收敛动作；等待/重试/Replan运行/未启动取消不计轮。"""

    if disposition == ProgressDispositionV1.PROGRESS:
        updated = state.model_copy(
            update={
                "current_loop_fingerprint": loop_fingerprint,
                "same_loop_no_progress": 0,
                "consecutive_no_progress": 0,
                "episode_replan_used": False,
                "episode_seen_loop_fingerprints": (loop_fingerprint,),
            }
        )
        return NoProgressRoundResultV1(action=NoProgressActionV1.CONTINUE, updated_state=updated)

    same_loop = (
        state.same_loop_no_progress + 1 if loop_fingerprint == state.current_loop_fingerprint else 1
    )
    consecutive = state.consecutive_no_progress + 1
    epoch = state.epoch_no_progress + 1
    task = state.task_no_progress + 1
    seen = state.episode_seen_loop_fingerprints
    if loop_fingerprint not in seen:
        seen = (*seen, loop_fingerprint)

    updated = state.model_copy(
        update={
            "current_loop_fingerprint": loop_fingerprint,
            "same_loop_no_progress": same_loop,
            "consecutive_no_progress": consecutive,
            "epoch_no_progress": epoch,
            "task_no_progress": task,
            "episode_seen_loop_fingerprints": seen,
        }
    )
    action = _converge_action(consecutive, epoch, task, disposition)
    return NoProgressRoundResultV1(action=action, updated_state=updated)


def replan_permitted(state: NoProgressStateV1, has_alternative_path: bool) -> bool:
    """每个无进展Episode最多1次Replan、每Task最多3次，且确有不同路径。"""

    return (
        has_alternative_path
        and not state.episode_replan_used
        and state.task_replan_count < MAX_REPLAN_PER_TASK
    )


def mark_replan_used(state: NoProgressStateV1) -> NoProgressStateV1:
    """执行一次Replan后记录Episode/任务级Replan消费；调用方先用 replan_permitted 把关。"""

    return state.model_copy(
        update={
            "episode_replan_used": True,
            "task_replan_count": state.task_replan_count + 1,
        }
    )


def is_probe_eligible(state: NoProgressStateV1, loop_fingerprint: Sha256Hex) -> bool:
    """新Plan只有产生当前Episode未见过的Loop Fingerprint才获得一个探测Wave。"""

    return loop_fingerprint not in state.episode_seen_loop_fingerprints


def new_input_epoch_reset(state: NoProgressStateV1) -> NoProgressStateV1:
    """新Task Input创建新Progress Epoch：清Epoch连续值、Task生命周期累计不清。"""

    return state.model_copy(update={"epoch_no_progress": 0})
