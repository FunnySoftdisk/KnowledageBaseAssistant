"""DEV-06：建议型Action候选、运行组限额画像与ID派生的离线验收；不接真实模型/PG。"""

import unittest
from uuid import UUID

from pydantic import ValidationError

from knowledge_system.modules.tasking.domain.action_candidate import (
    ACTION_ATTEMPT_NAMESPACE,
    ACTION_LIMIT_PROFILE_DEFAULT,
    ACTION_LIMIT_PROFILE_EXTERNAL_RESEARCH,
    ACTION_LIMIT_PROFILE_FACT_VERIFICATION,
    ACTION_LIMIT_PROFILE_KNOWLEDGE_RESEARCH,
    ACTION_LIMIT_PROFILE_SCIENTIFIC_ANALYSIS,
    ACTION_LIMIT_PROFILE_SYNTHESIS,
    ACTION_LIMIT_PROFILES,
    CAPABILITY_INVOCATION_NAMESPACE,
    ActionRunGroupV1,
    ActionRunLimitProfileV1,
    InlineJsonV1,
    ProposedActionV1,
    action_attempt_id,
    invocation_id,
    new_action_run_group,
)
from knowledge_system.modules.tasking.domain.core_types import ArtifactRefV1

ZERO_SHA = "0" * 64
TASK_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ATTEMPT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
GROUP_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
PLAN_ITEM_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
AGENT_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
SNAPSHOT_ID = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
ARTIFACT_ID = UUID("11111111-1111-4111-8111-111111111111")


def artifact_ref() -> ArtifactRefV1:
    return ArtifactRefV1(
        artifact_id=ARTIFACT_ID,
        artifact_type="TOOL_ARGUMENT",
        sha256=ZERO_SHA,
        schema_id="tool-argument",
        schema_version="1",
    )


def inline_payload() -> InlineJsonV1:
    return InlineJsonV1(
        payload_kind="INLINE_JSON",
        inline_json={"query": "q1", "limit": 3},
        artifact_ref=None,
        serialized_bytes=32,
    )


def proposed_action() -> ProposedActionV1:
    return ProposedActionV1(
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


def run_group() -> ActionRunGroupV1:
    return new_action_run_group(
        action_run_group_id=GROUP_ID,
        task_id=TASK_ID,
        plan_version=1,
        plan_item_id=PLAN_ITEM_ID,
        iteration_no=1,
        origin_task_attempt_id=ATTEMPT_ID,
        limit_profile=ACTION_LIMIT_PROFILE_DEFAULT,
    )


def _validate_mutated(model, **updates):
    """以 Python 对象形式重建并触发校验；model_copy(update=…) 不运行校验器。"""
    data = model.model_dump()
    data.update(updates)
    return type(model).model_validate(data)


class IdDerivationTests(unittest.TestCase):
    def test_namespaces_are_frozen(self) -> None:
        self.assertEqual(str(ACTION_ATTEMPT_NAMESPACE), "a0c8a059-9eea-5c1f-bbb0-93a8a95be3df")
        self.assertEqual(
            str(CAPABILITY_INVOCATION_NAMESPACE), "21a22dda-c061-5c2b-8029-4a76a94eb5ec"
        )

    def test_action_attempt_id_is_deterministic(self) -> None:
        first = action_attempt_id(ATTEMPT_ID, "call-1")
        second = action_attempt_id(ATTEMPT_ID, "call-1")
        self.assertEqual(first, second)
        self.assertNotEqual(first, action_attempt_id(ATTEMPT_ID, "call-2"))

    def test_action_attempt_id_rejects_empty_tool_call(self) -> None:
        with self.assertRaises(ValueError):
            action_attempt_id(ATTEMPT_ID, "")

    def test_invocation_id_derives_from_action_attempt(self) -> None:
        aa = action_attempt_id(ATTEMPT_ID, "call-1")
        self.assertEqual(invocation_id(aa), invocation_id(aa))
        other = invocation_id(action_attempt_id(ATTEMPT_ID, "call-2"))
        self.assertNotEqual(invocation_id(aa), other)


class InlineJsonTests(unittest.TestCase):
    def test_inline_json_branch(self) -> None:
        payload = inline_payload()
        self.assertEqual(payload.payload_kind, "INLINE_JSON")
        self.assertEqual(payload.inline_json["query"], "q1")

    def test_artifact_ref_branch(self) -> None:
        payload = InlineJsonV1(
            payload_kind="ARTIFACT_REF",
            inline_json=None,
            artifact_ref=artifact_ref(),
            serialized_bytes=0,
        )
        self.assertEqual(payload.artifact_ref.artifact_id, ARTIFACT_ID)

    def test_inline_json_requires_value(self) -> None:
        with self.assertRaises(ValidationError):
            InlineJsonV1(
                payload_kind="INLINE_JSON", inline_json=None, artifact_ref=None, serialized_bytes=0
            )

    def test_artifact_ref_requires_ref(self) -> None:
        with self.assertRaises(ValidationError):
            InlineJsonV1(
                payload_kind="ARTIFACT_REF", inline_json=None, artifact_ref=None, serialized_bytes=0
            )

    def test_mixed_branch_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            InlineJsonV1(
                payload_kind="INLINE_JSON",
                inline_json={"a": 1},
                artifact_ref=artifact_ref(),
                serialized_bytes=1,
            )

    def test_serialized_bytes_under_64kib(self) -> None:
        with self.assertRaises(ValidationError):
            InlineJsonV1(
                payload_kind="INLINE_JSON",
                inline_json={"a": 1},
                artifact_ref=None,
                serialized_bytes=65536,
            )


class ProposedActionTests(unittest.TestCase):
    def test_valid_proposal(self) -> None:
        proposal = proposed_action()
        self.assertEqual(proposal.plan_version, 1)
        self.assertEqual(proposal.model_tool_name, "search_internal_evidence_v1")
        self.assertEqual(proposal.arguments_payload.inline_json["limit"], 3)

    def test_plan_version_positive(self) -> None:
        with self.assertRaises(ValidationError):
            _validate_mutated(proposed_action(), plan_version=0)

    def test_tool_call_ordinal_positive(self) -> None:
        with self.assertRaises(ValidationError):
            _validate_mutated(proposed_action(), tool_call_ordinal=0)

    def test_extra_field_forbidden(self) -> None:
        with self.assertRaises(ValidationError):
            ProposedActionV1.model_validate(
                {**proposed_action().model_dump(), "capability_ref": "forged"}
            )


class LimitProfileTests(unittest.TestCase):
    def test_frozen_profiles_have_expected_limits(self) -> None:
        self.assertEqual(
            (
                ACTION_LIMIT_PROFILE_DEFAULT.max_tool_search_count,
                ACTION_LIMIT_PROFILE_DEFAULT.max_proposed_action_count,
                ACTION_LIMIT_PROFILE_DEFAULT.max_framework_tool_call_count,
                ACTION_LIMIT_PROFILE_DEFAULT.max_model_request_count,
            ),
            (4, 4, 8, 10),
        )
        self.assertEqual(
            (
                ACTION_LIMIT_PROFILE_KNOWLEDGE_RESEARCH.max_tool_search_count,
                ACTION_LIMIT_PROFILE_KNOWLEDGE_RESEARCH.max_proposed_action_count,
                ACTION_LIMIT_PROFILE_KNOWLEDGE_RESEARCH.max_framework_tool_call_count,
                ACTION_LIMIT_PROFILE_KNOWLEDGE_RESEARCH.max_model_request_count,
            ),
            (0, 4, 4, 7),
        )
        self.assertEqual(
            (
                ACTION_LIMIT_PROFILE_FACT_VERIFICATION.max_tool_search_count,
                ACTION_LIMIT_PROFILE_FACT_VERIFICATION.max_proposed_action_count,
                ACTION_LIMIT_PROFILE_FACT_VERIFICATION.max_framework_tool_call_count,
                ACTION_LIMIT_PROFILE_FACT_VERIFICATION.max_model_request_count,
            ),
            (0, 6, 6, 10),
        )
        self.assertEqual(
            (
                ACTION_LIMIT_PROFILE_EXTERNAL_RESEARCH.max_tool_search_count,
                ACTION_LIMIT_PROFILE_EXTERNAL_RESEARCH.max_proposed_action_count,
                ACTION_LIMIT_PROFILE_EXTERNAL_RESEARCH.max_framework_tool_call_count,
                ACTION_LIMIT_PROFILE_EXTERNAL_RESEARCH.max_model_request_count,
            ),
            (0, 9, 9, 12),
        )

    def test_all_six_profiles_registered(self) -> None:
        self.assertEqual(len(ACTION_LIMIT_PROFILES), 6)
        self.assertIn("scientific_analysis_action_v1", ACTION_LIMIT_PROFILES)
        self.assertIn("synthesis_action_v1", ACTION_LIMIT_PROFILES)

    def test_digest_is_stable_64_hex(self) -> None:
        self.assertEqual(len(ACTION_LIMIT_PROFILE_DEFAULT.profile_digest), 64)
        self.assertEqual(
            ACTION_LIMIT_PROFILE_DEFAULT.profile_digest,
            "d58eaeb95efb4afad95f38b775b9bc59b5686843bb4a56eb027090e663af47b0",
        )

    def test_synthesis_matches_knowledge_research_limits(self) -> None:
        self.assertEqual(
            ACTION_LIMIT_PROFILE_SCIENTIFIC_ANALYSIS.max_proposed_action_count,
            ACTION_LIMIT_PROFILE_KNOWLEDGE_RESEARCH.max_proposed_action_count,
        )
        self.assertEqual(
            ACTION_LIMIT_PROFILE_SYNTHESIS.max_framework_tool_call_count,
            ACTION_LIMIT_PROFILE_KNOWLEDGE_RESEARCH.max_framework_tool_call_count,
        )

    def test_framework_limit_must_equal_tool_plus_proposed(self) -> None:
        with self.assertRaises(ValidationError):
            ActionRunLimitProfileV1(
                profile_id="broken_action_v1",
                agent_registration_keys=(),
                max_tool_search_count=4,
                max_proposed_action_count=4,
                max_framework_tool_call_count=9,
                max_model_request_count=10,
                profile_digest=ZERO_SHA,
            )


class RunGroupTests(unittest.TestCase):
    def test_new_group_snapshots_limits_and_zero_counts(self) -> None:
        group = run_group()
        self.assertEqual(group.max_tool_search_count, 4)
        self.assertEqual(group.max_proposed_action_count, 4)
        self.assertEqual(group.limit_profile_digest, ACTION_LIMIT_PROFILE_DEFAULT.profile_digest)
        self.assertEqual(group.tool_search_count, 0)
        self.assertEqual(group.proposed_action_count, 0)
        self.assertEqual(group.framework_tool_call_count, 0)
        self.assertEqual(group.model_request_count, 0)
        self.assertEqual(group.execution_accepted_count, 0)
        self.assertEqual(group.completed_action_count, 0)

    def test_framework_count_mismatch_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _validate_mutated(
                run_group(),
                tool_search_count=1,
                proposed_action_count=1,
                framework_tool_call_count=1,
            )

    def test_proposed_action_limit_exceeded_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _validate_mutated(run_group(), proposed_action_count=5, framework_tool_call_count=5)

    def test_model_request_limit_exceeded_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _validate_mutated(run_group(), model_request_count=11)

    def test_execution_accepted_exceeds_proposed_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _validate_mutated(
                run_group(),
                proposed_action_count=2,
                framework_tool_call_count=2,
                execution_accepted_count=3,
            )

    def test_completed_exceeds_accepted_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _validate_mutated(
                run_group(),
                proposed_action_count=2,
                framework_tool_call_count=2,
                execution_accepted_count=1,
                completed_action_count=2,
            )

    def test_iteration_no_bounds(self) -> None:
        with self.assertRaises(ValidationError):
            _validate_mutated(run_group(), iteration_no=4)


if __name__ == "__main__":
    unittest.main()
