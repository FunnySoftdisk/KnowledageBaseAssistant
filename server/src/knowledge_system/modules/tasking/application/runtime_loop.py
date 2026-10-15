"""DEV-06 运行时循环接线：建议型 Action 执行循环 + 零 LLM Controller 派发 + Final 交接。

把「模型提出 Tool Call → ActionBinder 绑定 → 建议型执行 → Observation 不可变投影 → 结果采纳」
串成 `ActionRunService` 垂直切片，再用 `RuntimeLoopService` 把零 LLM `RuntimeDecisionController`
的决策派发到 Action 循环 / Final 综合 / 终态 / 等待四个分支，实现 Action→Observation→Controller→
Final 的运行时闭环接线。

服务自身零 LLM、零 I/O：提案由 `ActionProposalModelPort` 生成、建议型执行由 `ActionExecutorPort`
执行、Final 终态由 `FinalSynthesisService` 注入；绑定、采纳门禁、Observation 投影与决策派发全部
调用已冻结的确定性领域函数。运行组限额画像（`max_proposed_action_count`）在提案进入绑定前把关，
超限失败关闭；绑定失败不击穿循环，以 `ACTION_TERMINAL(FAILED)` Observation 记录后继续。只有
`ADOPTED` 终态的采纳引用进入 `ActionRunResult.adopted_refs`（即「当前激活 Plan 中已采纳的结果」），
`NOT_ADOPTED`/`FAILED` 与绑定失败一律不进入，由 Final 交接端据此只允许已采纳结果进入 Final。

本切片不落地 `RuntimeDecisionContextBuilder`（决策快照的 PG 事实投影）与无进展计数器的循环级合并
（`evaluate_no_progress_round` + 循环指纹）；`advance` 以注入的 `RuntimeDecisionSnapshotV1` 为输入，
循环的重复推进与快照重建由上层 Temporal Activity 驱动。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from knowledge_system.modules.tasking.domain.action_binding import (
    ActionBindingV1,
    CapabilityActionProfileV1,
    CapabilityBindingSnapshotV1,
    bind_action,
)
from knowledge_system.modules.tasking.domain.action_candidate import (
    ActionRunLimitProfileV1,
    ProposedActionV1,
    action_attempt_id,
)
from knowledge_system.modules.tasking.domain.core_types import (
    ArtifactRefV1,
    VersionedRefV1,
    canonical_json_sha256,
)
from knowledge_system.modules.tasking.domain.observation import (
    PROJECTION_PROFILE_VERSION,
    ActionTerminalPayloadV1,
    ObservationSourceKindV1,
    ResultAdoptedPayloadV1,
    ResultNotAdoptedPayloadV1,
    TaskObservationV1,
    observation_class_for,
    observation_id,
)
from knowledge_system.modules.tasking.domain.runtime_decision import (
    RuntimeDecisionSnapshotV1,
    RuntimeDecisionV1,
    RuntimeDirectiveActionV1,
    evaluate_runtime_decision,
)
from knowledge_system.modules.tasking.domain.runtime_progress import (
    ProgressDeltaKindV1,
    ProgressDispositionV1,
    classify_disposition,
)

from .final_synthesis import (
    FinalSynthesisMaterials,
    FinalSynthesisResult,
    FinalSynthesisService,
)

ActionTerminalStateV1 = Literal["ADOPTED", "NOT_ADOPTED", "FAILED"]
RuntimeLoopStepKindV1 = Literal["RUN_ACTIONS", "RUN_FINAL", "TERMINAL", "YIELD"]

_TERMINAL_ACTIONS: frozenset[RuntimeDirectiveActionV1] = frozenset(
    {
        RuntimeDirectiveActionV1.CLOSE_RUNTIME,
        RuntimeDirectiveActionV1.ADVANCE_CANCELLATION,
        RuntimeDirectiveActionV1.FAIL_TASK,
    }
)

_YIELD_ACTIONS: frozenset[RuntimeDirectiveActionV1] = frozenset(
    {
        RuntimeDirectiveActionV1.WAIT_RECONCILIATION,
        RuntimeDirectiveActionV1.REQUEST_USER_INPUT,
        RuntimeDirectiveActionV1.START_REPLAN,
        RuntimeDirectiveActionV1.RETRY_PLAN_ITEM,
        RuntimeDirectiveActionV1.WAIT_EVENT,
    }
)


class ActionProposalModelPort(Protocol):
    """建议型 Action 提案端口；实现负责把 Plan Item 上下文序列化为 Prompt 并跑真实模型。

    返回**已装配**的 `ProposedActionV1`（模型仅控制 `model_tool_name`/`arguments_payload`/
    `model_tool_call_id`，其余 Task/Plan/Item/Agent/Binding Snapshot 引用、ID 与 Digest 由
    ActionModel 适配器按服务端材料装配，见 `action_candidate.py` 约定）。
    """

    async def propose(self, materials: ActionRunMaterials) -> tuple[ProposedActionV1, ...]: ...


class ActionExecutorPort(Protocol):
    """建议型 Action 执行端口（本阶段不启用高风险副作用工具）。

    实现负责按已绑定 `ActionBindingV1` 做建议型执行并返回采纳/拒绝/失败终态；服务端完成
    Policy、预算、幂等与执行准入，模型与执行器均不得声明权威采纳事实。
    """

    async def execute(
        self, binding: ActionBindingV1, attempt_id: UUID
    ) -> ActionExecutionOutcomeV1: ...


@dataclass(frozen=True, slots=True)
class ActionExecutionOutcomeV1:
    """单次建议型执行的终态：采纳（携带受控引用）/ 拒绝 / 失败（携带原因码）。"""

    terminal_state: ActionTerminalStateV1
    adopted_refs: tuple[VersionedRefV1, ...] = ()
    reason_code: str = ""
    progress_delta_kinds: tuple[ProgressDeltaKindV1, ...] = ()
    regressed_refs: tuple[VersionedRefV1, ...] = ()

    def __post_init__(self) -> None:
        if self.terminal_state == "ADOPTED" and not self.adopted_refs:
            raise ValueError("ADOPTED_OUTCOME_REQUIRES_REFS")
        if self.terminal_state != "ADOPTED" and self.adopted_refs:
            raise ValueError("NON_ADOPTED_OUTCOME_REFS_FORBIDDEN")


@dataclass(frozen=True, slots=True)
class ActionRunMaterials:
    """Action 执行循环的权威材料：Attempt/运行组身份、绑定材料、执行准入画像与统一时钟。"""

    task_id: UUID
    task_attempt_id: UUID
    plan_version: int
    plan_item_id: UUID
    action_run_group_id: UUID
    agent_instance_id: UUID
    binding_snapshot: CapabilityBindingSnapshotV1
    action_profile: CapabilityActionProfileV1
    input_schema: dict[str, Any]
    proposal_ref: ArtifactRefV1
    purpose_code: str
    limit_profile: ActionRunLimitProfileV1
    source_event_id: UUID
    source_event_sequence: int
    now: datetime


@dataclass(frozen=True, slots=True)
class ActionAttemptResultV1:
    """单个提案的绑定/执行结果；`binding is None` 表示绑定失败，`outcome` 仅在绑定成功后非空。"""

    proposal: ProposedActionV1
    binding: ActionBindingV1 | None
    outcome: ActionExecutionOutcomeV1 | None = None
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ActionRunResult:
    """一次 Ready Set 执行循环的不可变结果；`adopted_refs` 只含 ADOPTED 终态的采纳引用。"""

    proposals: tuple[ProposedActionV1, ...]
    attempts: tuple[ActionAttemptResultV1, ...]
    observations: tuple[TaskObservationV1, ...]
    adopted_refs: tuple[VersionedRefV1, ...]
    disposition: ProgressDispositionV1


@dataclass(frozen=True, slots=True)
class RuntimeLoopStep:
    """零 LLM Controller 单次决策的派发结果；循环推进与快照重建由上层驱动。"""

    decision: RuntimeDecisionV1
    step_kind: RuntimeLoopStepKindV1
    action_run: ActionRunResult | None = None
    final_result: FinalSynthesisResult | None = None


def _bounded_code(value: str | None, default: str) -> str:
    code = value or default
    return code if len(code) <= 128 else code[:127] + "…"


def _build_observation(
    *,
    task_id: UUID,
    source_event_id: UUID,
    source_event_sequence: int,
    source_ordinal: int,
    source_plan_version: int,
    attempt: ActionAttemptResultV1,
    now: datetime,
) -> TaskObservationV1:
    proposal = attempt.proposal
    payload: ActionTerminalPayloadV1 | ResultAdoptedPayloadV1 | ResultNotAdoptedPayloadV1
    if attempt.binding is None:
        source_kind = ObservationSourceKindV1.ACTION_TERMINAL
        payload = ActionTerminalPayloadV1(
            source_kind="ACTION_TERMINAL",
            action_attempt_id=action_attempt_id(
                proposal.origin_task_attempt_id, proposal.model_tool_call_id
            ),
            terminal_state="FAILED",
            reason_code=_bounded_code(attempt.failure_reason, "BINDING_FAILED"),
        )
    elif attempt.outcome is None:
        raise ValueError("ACTION_ATTEMPT_MISSING_OUTCOME")
    elif attempt.outcome.terminal_state == "ADOPTED":
        source_kind = ObservationSourceKindV1.RESULT_ADOPTED
        payload = ResultAdoptedPayloadV1(
            source_kind="RESULT_ADOPTED", adopted_refs=attempt.outcome.adopted_refs
        )
    elif attempt.outcome.terminal_state == "NOT_ADOPTED":
        source_kind = ObservationSourceKindV1.RESULT_NOT_ADOPTED
        payload = ResultNotAdoptedPayloadV1(
            source_kind="RESULT_NOT_ADOPTED",
            outcome="NOT_ADOPTED",
            reason_code=_bounded_code(attempt.outcome.reason_code, "UNSPECIFIED"),
        )
    else:
        source_kind = ObservationSourceKindV1.ACTION_TERMINAL
        payload = ActionTerminalPayloadV1(
            source_kind="ACTION_TERMINAL",
            action_attempt_id=action_attempt_id(
                proposal.origin_task_attempt_id, proposal.model_tool_call_id
            ),
            terminal_state="FAILED",
            reason_code=_bounded_code(attempt.outcome.reason_code, "UNSPECIFIED"),
        )

    observation = TaskObservationV1(
        contract_version="task_observation_v1",
        observation_id=observation_id(
            task_id, source_event_id, source_ordinal, PROJECTION_PROFILE_VERSION
        ),
        task_id=task_id,
        source_event_id=source_event_id,
        source_event_sequence=source_event_sequence,
        source_ordinal=source_ordinal,
        source_kind=source_kind,
        observation_class=observation_class_for(source_kind),
        source_plan_version=source_plan_version,
        created_at=now,
        payload=payload,
        integrity_digest="0" * 64,
    )
    digest = canonical_json_sha256(
        observation.model_dump(mode="json", exclude={"integrity_digest"})
    )
    return observation.model_copy(update={"integrity_digest": digest})


def build_action_observations(
    *,
    task_id: UUID,
    source_event_id: UUID,
    source_event_sequence: int,
    source_plan_version: int,
    attempts: tuple[ActionAttemptResultV1, ...],
    now: datetime,
) -> tuple[TaskObservationV1, ...]:
    """把一次执行循环的 Attempt 结果投影为不可变 `TaskObservationV1`（每个 Attempt 一条）。"""

    if source_event_sequence < 1:
        raise ValueError("OBSERVATION_SOURCE_SEQUENCE_INVALID")
    observations: list[TaskObservationV1] = []
    for ordinal, attempt in enumerate(attempts):
        observations.append(
            _build_observation(
                task_id=task_id,
                source_event_id=source_event_id,
                source_event_sequence=source_event_sequence,
                source_ordinal=ordinal,
                source_plan_version=source_plan_version,
                attempt=attempt,
                now=now,
            )
        )
    return tuple(observations)


def _adopted_refs(attempts: list[ActionAttemptResultV1]) -> tuple[VersionedRefV1, ...]:
    refs: list[VersionedRefV1] = []
    seen: set[tuple[UUID, int, str]] = set()
    for attempt in attempts:
        if attempt.outcome is None or attempt.outcome.terminal_state != "ADOPTED":
            continue
        for ref in attempt.outcome.adopted_refs:
            key = (ref.ref_id, ref.version, ref.integrity_digest)
            if key not in seen:
                seen.add(key)
                refs.append(ref)
    return tuple(
        sorted(refs, key=lambda r: (str(r.ref_id).lower(), r.version, r.integrity_digest))
    )


def _delta_kinds(attempts: list[ActionAttemptResultV1]) -> tuple[ProgressDeltaKindV1, ...]:
    kinds: list[ProgressDeltaKindV1] = []
    seen: set[ProgressDeltaKindV1] = set()
    for attempt in attempts:
        if attempt.outcome is None:
            continue
        for kind in attempt.outcome.progress_delta_kinds:
            if kind not in seen:
                seen.add(kind)
                kinds.append(kind)
    return tuple(kinds)


def _regressed_refs(attempts: list[ActionAttemptResultV1]) -> tuple[VersionedRefV1, ...]:
    refs: list[VersionedRefV1] = []
    seen: set[tuple[UUID, int, str]] = set()
    for attempt in attempts:
        if attempt.outcome is None:
            continue
        for ref in attempt.outcome.regressed_refs:
            key = (ref.ref_id, ref.version, ref.integrity_digest)
            if key not in seen:
                seen.add(key)
                refs.append(ref)
    return tuple(
        sorted(refs, key=lambda r: (str(r.ref_id).lower(), r.version, r.integrity_digest))
    )


class ActionRunService:
    """建议型 Action 执行循环：提案 → 绑定 → 执行 → Observation → 采纳（零 LLM、零 I/O）。"""

    def __init__(self, model: ActionProposalModelPort, executor: ActionExecutorPort) -> None:
        self._model = model
        self._executor = executor

    async def run(self, materials: ActionRunMaterials) -> ActionRunResult:
        proposals = await self._model.propose(materials)
        if len(proposals) > materials.limit_profile.max_proposed_action_count:
            raise ValueError("PROPOSED_ACTION_LIMIT_EXCEEDED")

        attempts: list[ActionAttemptResultV1] = []
        for index, proposal in enumerate(proposals):
            try:
                binding = bind_action(
                    proposal=proposal,
                    snapshot=materials.binding_snapshot,
                    profile=materials.action_profile,
                    input_schema=materials.input_schema,
                    proposal_ref=materials.proposal_ref,
                    purpose_code=materials.purpose_code,
                    sequence_no=index + 1,
                )
            except ValueError as exc:
                attempts.append(
                    ActionAttemptResultV1(
                        proposal=proposal, binding=None, outcome=None, failure_reason=str(exc)
                    )
                )
                continue
            attempt_id = action_attempt_id(
                proposal.origin_task_attempt_id, proposal.model_tool_call_id
            )
            outcome = await self._executor.execute(binding, attempt_id)
            attempts.append(
                ActionAttemptResultV1(proposal=proposal, binding=binding, outcome=outcome)
            )

        observations = build_action_observations(
            task_id=materials.task_id,
            source_event_id=materials.source_event_id,
            source_event_sequence=materials.source_event_sequence,
            source_plan_version=materials.plan_version,
            attempts=tuple(attempts),
            now=materials.now,
        )
        return ActionRunResult(
            proposals=proposals,
            attempts=tuple(attempts),
            observations=observations,
            adopted_refs=_adopted_refs(attempts),
            disposition=classify_disposition(
                _delta_kinds(attempts), _regressed_refs(attempts)
            ),
        )


class RuntimeLoopService:
    """零 LLM Controller 单次决策派发：Action 循环 / Final 综合 / 终态 / 等待。"""

    def __init__(
        self, action_run: ActionRunService, final_synthesis: FinalSynthesisService
    ) -> None:
        self._action_run = action_run
        self._final_synthesis = final_synthesis

    async def advance(
        self,
        snapshot: RuntimeDecisionSnapshotV1,
        decision_sequence: int,
        *,
        action_materials: ActionRunMaterials | None = None,
        final_materials: FinalSynthesisMaterials | None = None,
    ) -> RuntimeLoopStep:
        decision = evaluate_runtime_decision(snapshot, decision_sequence)
        action = RuntimeDirectiveActionV1(decision.directive.action)

        if action is RuntimeDirectiveActionV1.SCHEDULE_READY_SET:
            if action_materials is None:
                raise ValueError("ACTION_MATERIALS_REQUIRED")
            action_result = await self._action_run.run(action_materials)
            return RuntimeLoopStep(
                decision=decision, step_kind="RUN_ACTIONS", action_run=action_result
            )

        if action is RuntimeDirectiveActionV1.START_FINAL_SYNTHESIS:
            if final_materials is None:
                raise ValueError("FINAL_MATERIALS_REQUIRED")
            final_result = await self._final_synthesis.finalize(final_materials)
            return RuntimeLoopStep(
                decision=decision, step_kind="RUN_FINAL", final_result=final_result
            )

        if action in _TERMINAL_ACTIONS:
            return RuntimeLoopStep(decision=decision, step_kind="TERMINAL")
        if action in _YIELD_ACTIONS:
            return RuntimeLoopStep(decision=decision, step_kind="YIELD")
        raise ValueError("RUNTIME_DIRECTIVE_UNKNOWN")


__all__ = [
    "ActionAttemptResultV1",
    "ActionExecutionOutcomeV1",
    "ActionExecutorPort",
    "ActionProposalModelPort",
    "ActionRunMaterials",
    "ActionRunResult",
    "ActionRunService",
    "ActionTerminalStateV1",
    "RuntimeLoopService",
    "RuntimeLoopStep",
    "RuntimeLoopStepKindV1",
    "build_action_observations",
]
