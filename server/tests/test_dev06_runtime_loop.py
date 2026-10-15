"""DEV-06 运行时循环接线离线验收：Action 执行循环 + 零 LLM Controller 派发 + Final 交接。

不接真实模型/PG/Blob：提案模型、建议型执行器、Final 模型与终态提交全部注入盲测替身。证明
`ActionRunService` 的提案→绑定→执行→Observation→采纳闭环（含绑定失败不击穿、限额失败关闭、
只有 ADOPTED 结果进入 `adopted_refs`），以及 `RuntimeLoopService` 把 Controller 决策派发到
Action 循环 / Final 综合 / 终态 / 等待四个分支（RUN_ACTIONS / RUN_FINAL / TERMINAL / YIELD）。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID

from knowledge_system.modules.tasking.application.final_commit import (
    TaskResultCommitDisposition,
    TaskResultCommitResult,
    TaskResultCommitV1,
)
from knowledge_system.modules.tasking.application.final_synthesis import (
    FinalSynthesisMaterials,
    FinalSynthesisService,
)
from knowledge_system.modules.tasking.application.runtime_loop import (
    ActionExecutionOutcomeV1,
    ActionRunMaterials,
    ActionRunService,
    RuntimeLoopService,
    build_action_observations,
)
from knowledge_system.modules.tasking.domain.action_binding import (
    ActionProjectionRuleV1,
    CapabilityActionProfileV1,
    CapabilityBindingSnapshotV1,
    CapabilityToolBindingV1,
)
from knowledge_system.modules.tasking.domain.action_candidate import (
    ACTION_LIMIT_PROFILE_DEFAULT,
    InlineJsonV1,
    ProposedActionV1,
)
from knowledge_system.modules.tasking.domain.compiled_criterion import (
    EVALUATOR_PROFILE_EVIDENCE_GROUNDED,
    CompiledCriterionV1,
    EvidenceSupportPredicateV1,
)
from knowledge_system.modules.tasking.domain.core_types import (
    ArtifactRefV1,
    CapabilityRefV1,
    PolicySnapshotRefV1,
    SchemaRefV1,
    VersionedRefV1,
)
from knowledge_system.modules.tasking.domain.final_admission import FinalSupportCatalogEntryV1
from knowledge_system.modules.tasking.domain.final_candidate import (
    FinalClaimDraftV1,
    FinalParagraphBlockV1,
    FinalSectionDraftV1,
    FinalSynthesisCandidateV1,
    SemanticCriterionAssessmentV1,
)
from knowledge_system.modules.tasking.domain.final_citation import CitationSourceV1
from knowledge_system.modules.tasking.domain.final_evaluation import CriterionEvaluationStateV1
from knowledge_system.modules.tasking.domain.final_synthesis import (
    FinalProtectedBudgetV1,
    FinalSynthesisSnapshotV1,
    final_synthesis_snapshot_id,
    freeze_final_synthesis_snapshot,
)
from knowledge_system.modules.tasking.domain.input_contracts import OutputContractV1
from knowledge_system.modules.tasking.domain.observation import (
    ActionTerminalPayloadV1,
    ObservationClassV1,
    ObservationSourceKindV1,
    ResultAdoptedPayloadV1,
    ResultNotAdoptedPayloadV1,
)
from knowledge_system.modules.tasking.domain.planning_fragments import CriterionSourceKindV1
from knowledge_system.modules.tasking.domain.runtime_decision import (
    DecisionReasonV1,
    FinalOutcomeKindV1,
    ReplanAdmissionV1,
    RuntimeDecisionSnapshotV1,
    TaskRuntimeStateV1,
    WakeSourceV1,
)
from knowledge_system.modules.tasking.domain.runtime_progress import (
    ProgressDeltaKindV1,
    ProgressDispositionV1,
)

ZERO_SHA = "0" * 64
TASK_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ATTEMPT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
GROUP_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
PLAN_ITEM_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
AGENT_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
SNAPSHOT_ID = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
ARTIFACT_ID = UUID("11111111-1111-4111-8111-111111111111")
TOOL_ID = UUID("22222222-2222-4222-8222-222222222222")
PROFILE_ID = UUID("33333333-3333-4333-8333-333333333333")
EVENT_ID = UUID("44444444-4444-4444-8444-444444444444")
INPUT_ID = UUID("55555555-5555-4555-8555-555555555555")
READY_SET_ID = UUID("66666666-6666-4666-8666-666666666666")
REF_ID = UUID("77777777-7777-4777-8777-777777777777")
REF_B_ID = UUID("88888888-8888-4888-8888-888888888888")

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def versioned_ref(ref_id: UUID) -> VersionedRefV1:
    return VersionedRefV1(ref_id=ref_id, version=1, integrity_digest=ZERO_SHA)


def artifact_ref() -> ArtifactRefV1:
    return ArtifactRefV1(
        artifact_id=ARTIFACT_ID,
        artifact_type="TOOL_ARGUMENT",
        sha256=ZERO_SHA,
        schema_id="tool-argument",
        schema_version="1",
    )


def capability_ref() -> CapabilityRefV1:
    return CapabilityRefV1(
        capability_id="search-internal-evidence",
        capability_version="1",
        integrity_digest=ZERO_SHA,
    )


def inline_payload(arguments: dict | None = None) -> InlineJsonV1:
    return InlineJsonV1(
        payload_kind="INLINE_JSON",
        inline_json=arguments if arguments is not None else ARGUMENTS,
        artifact_ref=None,
        serialized_bytes=64,
    )


ARGUMENTS = {
    "query": "internal evidence",
    "target": {"scheme": "https", "host": "Example.COM", "port": 443},
}

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "target": {
            "type": "object",
            "properties": {
                "scheme": {"type": "string"},
                "host": {"type": "string"},
                "port": {"type": "integer"},
            },
            "required": ["scheme", "host"],
            "additionalProperties": False,
        },
    },
    "required": ["query", "target"],
    "additionalProperties": False,
}


def proposed_action(**overrides) -> ProposedActionV1:
    kwargs = dict(
        contract_version="proposed_action_v1",
        task_id=TASK_ID,
        origin_task_attempt_id=ATTEMPT_ID,
        plan_version=1,
        plan_item_id=PLAN_ITEM_ID,
        action_run_group_id=GROUP_ID,
        agent_instance_id=AGENT_ID,
        binding_snapshot_id=SNAPSHOT_ID,
        binding_snapshot_digest=ZERO_SHA,
        origin_pydantic_run_id="run-1",
        model_step=1,
        model_tool_call_id="call-1",
        tool_call_ordinal=1,
        model_tool_name="search_internal_evidence_v1",
        arguments_payload=inline_payload(),
        arguments_digest=ZERO_SHA,
    )
    kwargs.update(overrides)
    return ProposedActionV1(**kwargs)


def tool_binding(**overrides) -> CapabilityToolBindingV1:
    kwargs = dict(
        capability_ref=capability_ref(),
        tool_id=TOOL_ID,
        catalog_version="1.2.0",
        model_tool_name="search_internal_evidence_v1",
        input_schema_digest=ZERO_SHA,
        output_schema_digest=ZERO_SHA,
        tool_policy_profile_ref=versioned_ref(UUID("99999999-9999-4999-8999-999999999999")),
        disclosure="EAGER",
        model_status="COMPATIBLE",
    )
    kwargs.update(overrides)
    return CapabilityToolBindingV1(**kwargs)


def binding_snapshot(**overrides) -> CapabilityBindingSnapshotV1:
    kwargs = dict(
        contract_version="capability_toolset_binding_v1",
        snapshot_id=SNAPSHOT_ID,
        task_id=TASK_ID,
        task_attempt_id=ATTEMPT_ID,
        agent_manifest_ref=versioned_ref(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaabbbb")),
        plan_version=1,
        parent_grant_refs=(),
        model_capability="claude-sonnet-5",
        provider_capabilities_digest=ZERO_SHA,
        bindings=(tool_binding(),),
        integrity_digest=ZERO_SHA,
    )
    kwargs.update(overrides)
    return CapabilityBindingSnapshotV1(**kwargs)


def action_profile(**overrides) -> CapabilityActionProfileV1:
    kwargs = dict(
        contract_version="capability_action_profile_v1",
        profile_id=PROFILE_ID,
        version=1,
        capability_ref=capability_ref(),
        tool_id=TOOL_ID,
        input_schema_digest=ZERO_SHA,
        action_family="search",
        risk_floor="R1",
        effect_class="READ_ONLY",
        idempotency_mode="READ",
        projection_rules=(
            ActionProjectionRuleV1(
                source_kind="ARGUMENT",
                source_pointer="/query",
                target_fact="DATA_INPUT",
                normalizer_id="opaque_string_v1",
                required=True,
            ),
            ActionProjectionRuleV1(
                source_kind="ARGUMENT",
                source_pointer="/target",
                target_fact="NETWORK_TARGET",
                normalizer_id="network_target_v1",
                required=True,
            ),
        ),
        semantic_argument_pointers=("/query",),
        effect_argument_pointers=(),
        normalizer_profile="default",
    )
    kwargs.update(overrides)
    return CapabilityActionProfileV1(**kwargs)


def action_materials(**overrides) -> ActionRunMaterials:
    kwargs = dict(
        task_id=TASK_ID,
        task_attempt_id=ATTEMPT_ID,
        plan_version=1,
        plan_item_id=PLAN_ITEM_ID,
        action_run_group_id=GROUP_ID,
        agent_instance_id=AGENT_ID,
        binding_snapshot=binding_snapshot(),
        action_profile=action_profile(),
        input_schema=INPUT_SCHEMA,
        proposal_ref=artifact_ref(),
        purpose_code="evidence-search",
        limit_profile=ACTION_LIMIT_PROFILE_DEFAULT,
        source_event_id=EVENT_ID,
        source_event_sequence=1,
        now=NOW,
    )
    kwargs.update(overrides)
    return ActionRunMaterials(**kwargs)


def adopted_outcome(
    ref: VersionedRefV1 | None = None,
    *,
    delta_kinds=(ProgressDeltaKindV1.NEW_ADOPTED_INFORMATION,),
) -> ActionExecutionOutcomeV1:
    chosen = ref if ref is not None else versioned_ref(REF_ID)
    return ActionExecutionOutcomeV1(
        terminal_state="ADOPTED", adopted_refs=(chosen,), progress_delta_kinds=delta_kinds
    )


class StubProposalModel:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def propose(self, materials):
        self.calls += 1
        index = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[index]


class StubExecutor:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0
        self.attempt_ids: list[UUID] = []

    async def execute(self, binding, attempt_id):
        self.calls += 1
        self.attempt_ids.append(attempt_id)
        index = min(self.calls - 1, len(self._outcomes) - 1)
        return self._outcomes[index]


class ActionRunServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_adopted_action_produces_adopted_observation(self):
        model = StubProposalModel([(proposed_action(),)])
        executor = StubExecutor([adopted_outcome()])
        result = await ActionRunService(model, executor).run(action_materials())

        self.assertEqual(result.adopted_refs, (versioned_ref(REF_ID),))
        self.assertEqual(result.disposition, ProgressDispositionV1.PROGRESS)
        observation = result.observations[0]
        self.assertEqual(observation.source_kind, ObservationSourceKindV1.RESULT_ADOPTED)
        self.assertEqual(observation.observation_class, ObservationClassV1.ADOPTED_RESULT)
        self.assertIsInstance(observation.payload, ResultAdoptedPayloadV1)
        self.assertEqual(observation.payload.adopted_refs, (versioned_ref(REF_ID),))
        self.assertEqual(len(observation.integrity_digest), 64)

    async def test_not_adopted_excluded_from_adopted_refs(self):
        outcome = ActionExecutionOutcomeV1(
            terminal_state="NOT_ADOPTED", reason_code="INSUFFICIENT_QUALITY"
        )
        result = await ActionRunService(
            StubProposalModel([(proposed_action(),)]), StubExecutor([outcome])
        ).run(action_materials())

        self.assertEqual(result.adopted_refs, ())
        self.assertEqual(result.disposition, ProgressDispositionV1.NO_PROGRESS)
        self.assertIsInstance(result.observations[0].payload, ResultNotAdoptedPayloadV1)

    async def test_failed_action_produces_terminal_observation(self):
        outcome = ActionExecutionOutcomeV1(terminal_state="FAILED", reason_code="PROVIDER_DOWN")
        result = await ActionRunService(
            StubProposalModel([(proposed_action(),)]), StubExecutor([outcome])
        ).run(action_materials())

        self.assertEqual(result.adopted_refs, ())
        self.assertIsInstance(result.observations[0].payload, ActionTerminalPayloadV1)
        self.assertEqual(result.observations[0].payload.terminal_state, "FAILED")

    async def test_binding_failure_records_terminal_not_crash(self):
        bad = proposed_action(model_tool_name="unknown_tool_v1", model_tool_call_id="call-bad")
        result = await ActionRunService(
            StubProposalModel([(bad,)]), StubExecutor([])
        ).run(action_materials())

        self.assertEqual(result.adopted_refs, ())
        payload = result.observations[0].payload
        self.assertIsInstance(payload, ActionTerminalPayloadV1)
        self.assertEqual(payload.terminal_state, "FAILED")
        self.assertEqual(payload.reason_code, "TOOL_NOT_DISCLOSED_IN_SNAPSHOT")

    async def test_proposal_limit_exceeded_fails_closed(self):
        proposals = tuple(proposed_action(model_tool_call_id=f"call-{i}") for i in range(5))
        with self.assertRaises(ValueError) as ctx:
            await ActionRunService(StubProposalModel([proposals]), StubExecutor([])).run(
                action_materials()
            )
        self.assertEqual(str(ctx.exception), "PROPOSED_ACTION_LIMIT_EXCEEDED")

    async def test_adoption_gate_only_adopted_results_flow(self):
        adopted = adopted_outcome()
        rejected = ActionExecutionOutcomeV1(terminal_state="NOT_ADOPTED", reason_code="DUP")
        failed = ActionExecutionOutcomeV1(terminal_state="FAILED", reason_code="TIMEOUT")
        proposals = (
            proposed_action(model_tool_call_id="call-1"),
            proposed_action(model_tool_call_id="call-2"),
            proposed_action(model_tool_call_id="call-3"),
        )
        result = await ActionRunService(
            StubProposalModel([proposals]), StubExecutor([adopted, rejected, failed])
        ).run(action_materials())

        self.assertEqual(result.adopted_refs, (versioned_ref(REF_ID),))
        kinds = [o.source_kind for o in result.observations]
        self.assertEqual(
            kinds,
            [
                ObservationSourceKindV1.RESULT_ADOPTED,
                ObservationSourceKindV1.RESULT_NOT_ADOPTED,
                ObservationSourceKindV1.ACTION_TERMINAL,
            ],
        )

    async def test_observations_have_distinct_ids(self):
        proposals = (
            proposed_action(model_tool_call_id="call-1"),
            proposed_action(model_tool_call_id="call-2"),
        )
        result = await ActionRunService(
            StubProposalModel([proposals]),
            StubExecutor([adopted_outcome(), adopted_outcome(versioned_ref(REF_B_ID))]),
        ).run(action_materials())

        ids = [o.observation_id for o in result.observations]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(result.adopted_refs, (versioned_ref(REF_ID), versioned_ref(REF_B_ID)))

    async def test_adopted_requires_refs(self):
        with self.assertRaises(ValueError):
            ActionExecutionOutcomeV1(terminal_state="ADOPTED")

    async def test_non_adopted_forbids_refs(self):
        with self.assertRaises(ValueError):
            ActionExecutionOutcomeV1(terminal_state="FAILED", adopted_refs=(versioned_ref(REF_ID),))

    def test_build_observations_rejects_zero_sequence(self):
        attempt = next(iter(self._attempt(adopted_outcome())))
        with self.assertRaises(ValueError):
            build_action_observations(
                task_id=TASK_ID,
                source_event_id=EVENT_ID,
                source_event_sequence=0,
                source_plan_version=1,
                attempts=(attempt,),
                now=NOW,
            )

    @staticmethod
    def _attempt(outcome):
        from knowledge_system.modules.tasking.application.runtime_loop import (
            ActionAttemptResultV1,
        )

        return (ActionAttemptResultV1(proposal=proposed_action(), binding=None, outcome=outcome),)


class RuntimeLoopServiceTests(unittest.IsolatedAsyncioTestCase):
    async def _loop(self, final_materials=None):
        model = StubProposalModel([()])
        executor = StubExecutor([])
        action_run = ActionRunService(model, executor)
        final_synthesis = FinalSynthesisService(StubFinalModel([]), RecordingCommitWriter())
        return RuntimeLoopService(action_run, final_synthesis), final_materials

    async def test_schedule_ready_set_runs_actions(self):
        loop, _ = await self._loop()
        step = await loop.advance(
            _decision_snapshot(
                has_ready_items=True, ready_set_ref=versioned_ref(READY_SET_ID)
            ),
            1,
            action_materials=action_materials(),
        )
        self.assertEqual(step.step_kind, "RUN_ACTIONS")
        self.assertIsNotNone(step.action_run)
        self.assertEqual(step.decision.directive.action, "SCHEDULE_READY_SET")

    async def test_start_final_synthesis_runs_final(self):
        snap = _final_snapshot()
        candidate = _candidate(snap)
        final_synthesis = FinalSynthesisService(
            StubFinalModel([candidate]), RecordingCommitWriter()
        )
        loop = RuntimeLoopService(
            ActionRunService(StubProposalModel([()]), StubExecutor([])), final_synthesis
        )
        step = await loop.advance(
            _decision_snapshot(
                final_outcome_kind=FinalOutcomeKindV1.COMPLETE,
                finalization_reason=DecisionReasonV1.REQUIRED_CRITERION_SATISFIED,
                contributions_merged=True,
            ),
            2,
            final_materials=_final_materials(snap),
        )
        self.assertEqual(step.step_kind, "RUN_FINAL")
        self.assertEqual(step.final_result.outcome, "FINAL_COMMITTED")
        self.assertEqual(step.final_result.answer_status, FinalOutcomeKindV1.COMPLETE)

    async def test_fail_task_is_terminal(self):
        loop, _ = await self._loop()
        step = await loop.advance(
            _decision_snapshot(fail_reason=DecisionReasonV1.INTEGRITY_CORRUPTION), 1
        )
        self.assertEqual(step.step_kind, "TERMINAL")
        self.assertEqual(step.decision.directive.action, "FAIL_TASK")

    async def test_user_cancel_is_terminal(self):
        loop, _ = await self._loop()
        step = await loop.advance(
            _decision_snapshot(
                task_state=TaskRuntimeStateV1.CANCEL_REQUESTED,
                has_pending_cancellation=True,
                cancellation_terminal_kind="CANCELLED",
            ),
            1,
        )
        self.assertEqual(step.step_kind, "TERMINAL")
        self.assertEqual(step.decision.directive.action, "ADVANCE_CANCELLATION")

    async def test_wait_event_yields(self):
        loop, _ = await self._loop()
        step = await loop.advance(
            _decision_snapshot(wake_sources=(_wake_source(),)), 1
        )
        self.assertEqual(step.step_kind, "YIELD")
        self.assertEqual(step.decision.directive.action, "WAIT_EVENT")

    async def test_schedule_requires_materials(self):
        loop, _ = await self._loop()
        with self.assertRaises(ValueError) as ctx:
            await loop.advance(
                _decision_snapshot(
                    has_ready_items=True, ready_set_ref=versioned_ref(READY_SET_ID)
                ),
                1,
            )
        self.assertEqual(str(ctx.exception), "ACTION_MATERIALS_REQUIRED")

    async def test_final_requires_materials(self):
        loop, _ = await self._loop()
        with self.assertRaises(ValueError) as ctx:
            await loop.advance(
                _decision_snapshot(
                    final_outcome_kind=FinalOutcomeKindV1.PARTIAL,
                    finalization_reason=DecisionReasonV1.PARTIAL_RESULT_ONLY,
                    contributions_merged=True,
                ),
                1,
            )
        self.assertEqual(str(ctx.exception), "FINAL_MATERIALS_REQUIRED")


class StubFinalModel:
    def __init__(self, responses):
        self._responses = list(responses)

    async def generate_candidate(self, snapshot, catalog, repair_hints=()):
        return self._responses[0]


class RecordingCommitWriter:
    def __init__(self):
        self.commits: list[TaskResultCommitV1] = []

    async def commit(self, commit: TaskResultCommitV1) -> TaskResultCommitResult:
        self.commits.append(commit)
        return TaskResultCommitResult(
            disposition=TaskResultCommitDisposition.INSERTED,
            task_id=commit.task_id,
            result_version=commit.result_version,
            result_commit_digest=commit.result_commit_digest,
            row_version=1,
        )


def _wake_source() -> WakeSourceV1:
    return WakeSourceV1(kind="BUSINESS_BACKOFF", refs=(), next_wakeup_at=NOW + timedelta(hours=1))


def _decision_snapshot(**overrides) -> RuntimeDecisionSnapshotV1:
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
        deadline_at=NOW + timedelta(hours=6),
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


def _vref(ref_id: UUID) -> VersionedRefV1:
    return VersionedRefV1(ref_id=ref_id, version=1, integrity_digest=ZERO_SHA)


def _criterion() -> CompiledCriterionV1:
    return CompiledCriterionV1(
        criterion_id=UUID("cccccccc-cccc-4ccc-8ccc-ccccccccdddd"),
        statement="关键事实有据可查",
        source_kind=CriterionSourceKindV1.GOAL,
        source_refs=("gc1",),
        requirement_level="REQUIRED",
        completion_role="CORE_DELIVERABLE",
        protected=True,
        evaluation_mode="EVIDENCE_GROUNDED_SEMANTIC",
        predicate=EvidenceSupportPredicateV1(
            kind="EVIDENCE_SUPPORT", producer_plan_item_id=PLAN_ITEM_ID, min_citations=1
        ),
        applicability=None,
        evaluator_profile=EVALUATOR_PROFILE_EVIDENCE_GROUNDED,
        digest=ZERO_SHA,
    )


def _contract() -> OutputContractV1:
    return OutputContractV1(
        format="MARKDOWN",
        delivery="INLINE",
        language="zh-CN",
        schema_ref=None,
        template_or_skill_ref=None,
        citations="OPTIONAL",
        max_output_bytes=100_000,
    )


def _final_snapshot() -> FinalSynthesisSnapshotV1:
    return freeze_final_synthesis_snapshot(
        FinalSynthesisSnapshotV1(
            contract_version="final_synthesis_snapshot_v1",
            task_id=TASK_ID,
            runtime_decision_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbcccc"),
            input_id=INPUT_ID,
            plan_version=1,
            work_version=1,
            execution_generation=0,
            cancellation_generation=0,
            event_high_watermark=0,
            compiled_criteria=(_criterion(),),
            effective_constraint_refs=(),
            output_contract=_contract(),
            adopted_result_refs=(),
            gap_summaries=(),
            conflict_ids=(),
            unresolved_subgoal_ids=(),
            permanent_blocker_ids=(),
            citation_candidate_catalog_ids=(),
            result_artifact_candidate_catalog_ids=(),
            policy_snapshot_ref=PolicySnapshotRefV1(
                policy_snapshot_id=UUID("dddddddd-dddd-4ddd-8ddd-ddddddddcccc"),
                policy_revision=1,
                policy_digest=ZERO_SHA,
                observed_at=NOW,
            ),
            source_visibility=(),
            authorization_revoked=False,
            degraded=False,
            final_protected_budget=FinalProtectedBudgetV1(
                model_requests=2, input_tokens=0, output_tokens=0
            ),
            deadline_at=NOW + timedelta(hours=6),
            builder_profile="final-synthesis-context-builder-v1",
            builder_schema_ref=SchemaRefV1(
                schema_id="final-synthesis", schema_version="v1", schema_digest=ZERO_SHA
            ),
            snapshot_digest=ZERO_SHA,
        )
    )


def _snap_id(snap: FinalSynthesisSnapshotV1) -> UUID:
    return final_synthesis_snapshot_id(
        task_id=snap.task_id,
        runtime_decision_id=snap.runtime_decision_id,
        snapshot_digest=snap.snapshot_digest,
        builder_profile=snap.builder_profile,
    )


def _claim(local_id, kind, text, *, support=()) -> FinalClaimDraftV1:
    return FinalClaimDraftV1(
        local_claim_id=local_id,
        claim_kind=kind,
        text=text,
        support_catalog_ids=support,
        criterion_ids=(),
        conflict_set_ids=(),
    )


def _candidate(snap: FinalSynthesisSnapshotV1) -> FinalSynthesisCandidateV1:
    criterion_id = snap.compiled_criteria[0].criterion_id
    return FinalSynthesisCandidateV1(
        contract_version="final_synthesis_v1",
        snapshot_id=_snap_id(snap),
        snapshot_digest=snap.snapshot_digest,
        title="报告标题",
        claims=(_claim("c1", "SOURCE_FACT", "事实A", support=("vc1",)),),
        sections=(
            FinalSectionDraftV1(
                local_section_id="s1",
                heading="概述",
                blocks=(FinalParagraphBlockV1(block_kind="PARAGRAPH", claim_local_ids=("c1",)),),
            ),
        ),
        semantic_criterion_assessments=(
            SemanticCriterionAssessmentV1(
                criterion_id=criterion_id,
                assessment="SUPPORTED",
                supporting_catalog_ids=("vc1",),
                claim_local_ids=("c1",),
                note="证据支持",
            ),
        ),
        gap_reports=(),
        selected_result_artifact_catalog_ids=(),
    )


def _catalog() -> tuple[FinalSupportCatalogEntryV1, ...]:
    return (
        FinalSupportCatalogEntryV1(
            catalog_id="vc1",
            kind="VERIFIED_CLAIM",
            source_ref=_vref(UUID("99999999-9999-4999-8999-999999999998")),
            canonical_text="事实A",
            verdict="SUPPORTED",
            artifact_kind=None,
            evidence_ref=_vref(REF_ID),
        ),
    )


def _sources() -> tuple[CitationSourceV1, ...]:
    return (
        CitationSourceV1(
            evidence_ref_id=REF_ID,
            source_domain="EXTERNAL_WEB",
            locator_kind="WEB",
            locator_value="https://example.com",
            preview=None,
        ),
    )


def _final_materials(snap: FinalSynthesisSnapshotV1) -> FinalSynthesisMaterials:
    return FinalSynthesisMaterials(
        snapshot=snap,
        catalog=_catalog(),
        citation_sources=_sources(),
        evaluation_state=CriterionEvaluationStateV1(
            completed_item_ids=(), adopted_outputs=(), constraint_checks=()
        ),
        degradation_flags=(),
        output_contract_complete=True,
        final_attempt_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaacccc"),
        candidate_artifact_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbdddd"),
        validation_artifact_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccceeee"),
        criterion_evaluation_set_artifact_id=UUID("dddddddd-dddd-4ddd-8ddd-ddddddddffff"),
        completion_gate_decision_artifact_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeee0001"),
        generated_at=NOW,
    )


if __name__ == "__main__":
    unittest.main()
