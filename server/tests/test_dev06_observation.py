"""DEV-06：Observation 不可变投影与 Disposition 的离线验收；不接真实模型/PG。"""

import unittest
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError

from knowledge_system.modules.tasking.domain.core_types import (
    CapabilityRefV1,
    VersionedRefV1,
)
from knowledge_system.modules.tasking.domain.observation import (
    OBSERVATION_PAYLOAD_ADAPTER,
    PROJECTION_PROFILE_VERSION,
    TASK_OBSERVATION_NAMESPACE,
    ActionTerminalPayloadV1,
    AgentAssessmentReportedPayloadV1,
    AgentReportItemV1,
    ApprovalTerminalPayloadV1,
    AttemptAssessmentV1,
    BudgetThresholdReachedPayloadV1,
    CapabilityAvailabilityChangedPayloadV1,
    ExternalEffectChangedPayloadV1,
    ObservationClassV1,
    ObservationDispositionEventV1,
    ObservationDispositionV1,
    ObservationSourceKindV1,
    ResourceAuthorizationChangedPayloadV1,
    ResultAdoptedPayloadV1,
    ResultNotAdoptedPayloadV1,
    TaskDeadlineReachedPayloadV1,
    TaskInputAdvancedPayloadV1,
    TaskObservationV1,
    observation_class_for,
    observation_id,
    observation_sort_key,
)

ZERO_SHA = "0" * 64
TASK_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
EVENT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
ATTEMPT_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
GROUP_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
PLAN_ITEM_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
APPROVAL_ID = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")


def versioned_ref(ref_id: UUID = APPROVAL_ID) -> VersionedRefV1:
    return VersionedRefV1(ref_id=ref_id, version=1, integrity_digest=ZERO_SHA)


def capability_ref() -> CapabilityRefV1:
    return CapabilityRefV1(
        capability_id="writing-v1", capability_version="1", integrity_digest=ZERO_SHA
    )


def created_at() -> datetime:
    return datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


def assessment() -> AttemptAssessmentV1:
    return AttemptAssessmentV1(
        contract_version="attempt_assessment_v1",
        task_attempt_id=ATTEMPT_ID,
        plan_version=1,
        plan_item_id=PLAN_ITEM_ID,
        action_run_group_id=GROUP_ID,
        progress="NO_PROGRESS",
        report_items=(),
        claimed_satisfied_criterion_refs=(),
        supporting_result_refs=(),
        assessment_digest=ZERO_SHA,
    )


def make_observation(
    source_kind: ObservationSourceKindV1,
    payload: object,
    *,
    source_ordinal: int = 0,
    source_event_sequence: int = 1,
    source_event_id: UUID = EVENT_ID,
) -> TaskObservationV1:
    return TaskObservationV1(
        contract_version="task_observation_v1",
        observation_id=observation_id(
            TASK_ID, source_event_id, source_ordinal, PROJECTION_PROFILE_VERSION
        ),
        task_id=TASK_ID,
        source_event_id=source_event_id,
        source_event_sequence=source_event_sequence,
        source_ordinal=source_ordinal,
        source_kind=source_kind,
        observation_class=observation_class_for(source_kind),
        source_plan_version=1,
        created_at=created_at(),
        payload=payload,
        integrity_digest=ZERO_SHA,
    )


class ObservationClassMappingTests(unittest.TestCase):
    def test_special_sources_map_to_special_classes(self) -> None:
        self.assertEqual(
            observation_class_for(ObservationSourceKindV1.RESULT_ADOPTED),
            ObservationClassV1.ADOPTED_RESULT,
        )
        self.assertEqual(
            observation_class_for(ObservationSourceKindV1.TASK_INPUT_ADVANCED),
            ObservationClassV1.USER_INPUT,
        )
        self.assertEqual(
            observation_class_for(ObservationSourceKindV1.AGENT_ASSESSMENT_REPORTED),
            ObservationClassV1.AGENT_ASSESSMENT,
        )

    def test_eight_remaining_sources_map_to_system_fact(self) -> None:
        system_fact_sources = [
            ObservationSourceKindV1.RESULT_NOT_ADOPTED,
            ObservationSourceKindV1.ACTION_TERMINAL,
            ObservationSourceKindV1.APPROVAL_TERMINAL,
            ObservationSourceKindV1.EXTERNAL_EFFECT_CHANGED,
            ObservationSourceKindV1.RESOURCE_AUTHORIZATION_CHANGED,
            ObservationSourceKindV1.CAPABILITY_AVAILABILITY_CHANGED,
            ObservationSourceKindV1.BUDGET_THRESHOLD_REACHED,
            ObservationSourceKindV1.TASK_DEADLINE_REACHED,
        ]
        for source in system_fact_sources:
            self.assertEqual(observation_class_for(source), ObservationClassV1.SYSTEM_FACT)

    def test_all_eleven_sources_are_covered(self) -> None:
        self.assertEqual(len(ObservationSourceKindV1), 11)


class ObservationIdTests(unittest.TestCase):
    def test_namespace_is_frozen(self) -> None:
        self.assertEqual(str(TASK_OBSERVATION_NAMESPACE), "ecdd6256-b28a-5367-8040-a4100cc0776b")

    def test_id_is_deterministic(self) -> None:
        first = observation_id(TASK_ID, EVENT_ID, 0, PROJECTION_PROFILE_VERSION)
        second = observation_id(TASK_ID, EVENT_ID, 0, PROJECTION_PROFILE_VERSION)
        self.assertEqual(first, second)
        self.assertNotEqual(
            first, observation_id(TASK_ID, EVENT_ID, 1, PROJECTION_PROFILE_VERSION)
        )

    def test_negative_ordinal_rejected(self) -> None:
        with self.assertRaises(ValueError):
            observation_id(TASK_ID, EVENT_ID, -1, PROJECTION_PROFILE_VERSION)


class TaskObservationTests(unittest.TestCase):
    def test_adopted_result_payload(self) -> None:
        payload = ResultAdoptedPayloadV1(
            source_kind="RESULT_ADOPTED", adopted_refs=(versioned_ref(),)
        )
        observation = make_observation(ObservationSourceKindV1.RESULT_ADOPTED, payload)
        self.assertEqual(observation.observation_class, ObservationClassV1.ADOPTED_RESULT)
        self.assertEqual(observation.payload.adopted_refs[0].ref_id, APPROVAL_ID)

    def test_adopted_result_requires_refs(self) -> None:
        with self.assertRaises(ValidationError):
            ResultAdoptedPayloadV1(source_kind="RESULT_ADOPTED", adopted_refs=())

    def test_system_fact_payloads_roundtrip(self) -> None:
        cases = [
            ResultNotAdoptedPayloadV1(
                source_kind="RESULT_NOT_ADOPTED", outcome="REJECTED_STALE", reason_code="STALE"
            ),
            ActionTerminalPayloadV1(
                source_kind="ACTION_TERMINAL",
                action_attempt_id=ATTEMPT_ID,
                terminal_state="COMPLETED",
                reason_code=None,
            ),
            ApprovalTerminalPayloadV1(
                source_kind="APPROVAL_TERMINAL",
                approval_ref=versioned_ref(),
                decision="APPROVED",
                reason_code=None,
            ),
            ExternalEffectChangedPayloadV1(
                source_kind="EXTERNAL_EFFECT_CHANGED",
                invocation_id=ATTEMPT_ID,
                effect_state="RECONCILED",
                reason_code=None,
            ),
            ResourceAuthorizationChangedPayloadV1(
                source_kind="RESOURCE_AUTHORIZATION_CHANGED",
                resource_ref=versioned_ref(),
                authorization_state="REVOKED",
                reason_code=None,
            ),
            CapabilityAvailabilityChangedPayloadV1(
                source_kind="CAPABILITY_AVAILABILITY_CHANGED",
                capability_ref=capability_ref(),
                availability_state="UNAVAILABLE",
                reason_code=None,
            ),
            BudgetThresholdReachedPayloadV1(
                source_kind="BUDGET_THRESHOLD_REACHED",
                budget_kind="MODEL_REQUEST",
                threshold_state="EXHAUSTED",
            ),
            TaskDeadlineReachedPayloadV1(
                source_kind="TASK_DEADLINE_REACHED",
                deadline_at=created_at(),
                reason_code="TASK_DEADLINE_EXCEEDED",
            ),
        ]
        sources = [
            ObservationSourceKindV1.RESULT_NOT_ADOPTED,
            ObservationSourceKindV1.ACTION_TERMINAL,
            ObservationSourceKindV1.APPROVAL_TERMINAL,
            ObservationSourceKindV1.EXTERNAL_EFFECT_CHANGED,
            ObservationSourceKindV1.RESOURCE_AUTHORIZATION_CHANGED,
            ObservationSourceKindV1.CAPABILITY_AVAILABILITY_CHANGED,
            ObservationSourceKindV1.BUDGET_THRESHOLD_REACHED,
            ObservationSourceKindV1.TASK_DEADLINE_REACHED,
        ]
        for source, payload in zip(sources, cases, strict=True):
            observation = make_observation(source, payload)
            self.assertEqual(observation.observation_class, ObservationClassV1.SYSTEM_FACT)
            loaded = OBSERVATION_PAYLOAD_ADAPTER.validate_json(
                OBSERVATION_PAYLOAD_ADAPTER.dump_json(payload)
            )
            self.assertEqual(loaded, payload)

    def test_user_input_payload(self) -> None:
        payload = TaskInputAdvancedPayloadV1(
            source_kind="TASK_INPUT_ADVANCED",
            input_id=TASK_ID,
            input_version=1,
            supersedes_input_id=None,
        )
        observation = make_observation(ObservationSourceKindV1.TASK_INPUT_ADVANCED, payload)
        self.assertEqual(observation.observation_class, ObservationClassV1.USER_INPUT)

    def test_agent_assessment_payload(self) -> None:
        payload = AgentAssessmentReportedPayloadV1(
            source_kind="AGENT_ASSESSMENT_REPORTED", assessment=assessment()
        )
        observation = make_observation(
            ObservationSourceKindV1.AGENT_ASSESSMENT_REPORTED, payload
        )
        self.assertEqual(
            observation.observation_class, ObservationClassV1.AGENT_ASSESSMENT
        )
        self.assertEqual(observation.payload.assessment.progress, "NO_PROGRESS")

    def test_rejects_class_source_mismatch(self) -> None:
        payload = ResultAdoptedPayloadV1(
            source_kind="RESULT_ADOPTED", adopted_refs=(versioned_ref(),)
        )
        with self.assertRaises(ValidationError):
            TaskObservationV1(
                **{
                    **make_observation(
                        ObservationSourceKindV1.RESULT_ADOPTED, payload
                    ).model_dump(mode="json"),
                    "observation_class": "SYSTEM_FACT",
                }
            )

    def test_rejects_payload_source_mismatch(self) -> None:
        payload = ResultAdoptedPayloadV1(
            source_kind="RESULT_ADOPTED", adopted_refs=(versioned_ref(),)
        )
        with self.assertRaises(ValidationError):
            TaskObservationV1(
                **{
                    **make_observation(
                        ObservationSourceKindV1.RESULT_ADOPTED, payload
                    ).model_dump(mode="json"),
                    "source_kind": "TASK_INPUT_ADVANCED",
                }
            )

    def test_payload_extra_field_forbidden(self) -> None:
        with self.assertRaises(ValidationError):
            ResultNotAdoptedPayloadV1(
                source_kind="RESULT_NOT_ADOPTED",
                outcome="REJECTED_STALE",
                reason_code="STALE",
                leaked_content="must not pass",  # type: ignore[call-arg]
            )

    def test_deadline_normalized_to_utc(self) -> None:
        from datetime import timedelta, timezone

        payload = TaskDeadlineReachedPayloadV1(
            source_kind="TASK_DEADLINE_REACHED",
            deadline_at=datetime(
                2026, 10, 1, 20, 0, 0, tzinfo=timezone(timedelta(hours=8))
            ),
            reason_code="TASK_DEADLINE_EXCEEDED",
        )
        self.assertEqual(payload.deadline_at.tzinfo, UTC)
        self.assertEqual(payload.deadline_at.hour, 12)

    def test_deadline_rejects_naive(self) -> None:
        with self.assertRaises(ValidationError):
            TaskDeadlineReachedPayloadV1(
                source_kind="TASK_DEADLINE_REACHED",
                deadline_at=datetime(2026, 10, 1, 20, 0, 0),
                reason_code="TASK_DEADLINE_EXCEEDED",
            )

    def test_sort_key_orders_by_sequence_ordinal_id(self) -> None:
        payload = ResultAdoptedPayloadV1(
            source_kind="RESULT_ADOPTED", adopted_refs=(versioned_ref(),)
        )
        early = make_observation(
            ObservationSourceKindV1.RESULT_ADOPTED, payload, source_event_sequence=1
        )
        late = make_observation(
            ObservationSourceKindV1.RESULT_ADOPTED, payload, source_event_sequence=2
        )
        self.assertLess(observation_sort_key(early), observation_sort_key(late))


class AssessmentTests(unittest.TestCase):
    def test_report_item_limits(self) -> None:
        with self.assertRaises(ValidationError):
            AttemptAssessmentV1(
                contract_version="attempt_assessment_v1",
                task_attempt_id=ATTEMPT_ID,
                plan_version=1,
                plan_item_id=PLAN_ITEM_ID,
                action_run_group_id=GROUP_ID,
                progress="PARTIAL",
                report_items=tuple(
                    AgentReportItemV1(
                        kind="EVIDENCE_GAP", statement=f"gap {i}", refs=()
                    )
                    for i in range(17)
                ),
                claimed_satisfied_criterion_refs=(),
                supporting_result_refs=(),
                assessment_digest=ZERO_SHA,
            )

    def test_statement_length_limited(self) -> None:
        with self.assertRaises(ValidationError):
            AgentReportItemV1(kind="CONFLICT", statement="x" * 513, refs=())

    def test_progress_enum_rejects_unknown(self) -> None:
        with self.assertRaises(ValidationError):
            AttemptAssessmentV1(
                **{
                    **assessment().model_dump(mode="json"),
                    "progress": "DONE",
                }
            )


class DispositionTests(unittest.TestCase):
    def test_three_dispositions_allowed(self) -> None:
        for disposition in ObservationDispositionV1:
            event = ObservationDispositionEventV1(
                contract_version="observation_disposition_event_v1",
                disposition_event_id=ATTEMPT_ID,
                observation_id=observation_id(
                    TASK_ID, EVENT_ID, 0, PROJECTION_PROFILE_VERSION
                ),
                task_id=TASK_ID,
                disposition=disposition,
                reason_code="SUPERSEDED_BY_NEWER_FACT",
                caused_by_event_id=EVENT_ID,
                created_at=created_at(),
            )
            self.assertEqual(event.disposition, disposition)


if __name__ == "__main__":
    unittest.main()
