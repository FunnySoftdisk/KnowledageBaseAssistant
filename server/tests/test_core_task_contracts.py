"""CORE基础Ref、入口与Goal字段的确定性负向测试。"""

import json
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from knowledge_system.modules.tasking.domain.core_types import (
    ArtifactRefV1,
    PolicySnapshotRefV1,
    SchemaRefV1,
    canonical_json_sha256,
)
from knowledge_system.modules.tasking.domain.goal_contracts import (
    GoalGateDecisionV1,
    GoalSemanticReasonV1,
    GoalUnderstandingContextV1,
    GoalUnderstandingV1,
    validate_goal_relations,
)
from knowledge_system.modules.tasking.domain.input_contracts import (
    CreateTaskAttachmentV1,
    CreateTaskRequestV1,
    KnowledgeScopeModeV1,
    KnowledgeScopeV1,
    NetworkPolicySourceV1,
    OutputContractV1,
    TaskInputSnapshotV1,
    create_task_request_digest,
    normalize_knowledge_scope,
    normalize_network_control,
    parse_create_task_request,
)

ZERO_SHA = "0" * 64


class CoreTaskContractTests(unittest.TestCase):
    def test_reference_is_closed_strict_and_utc(self) -> None:
        with self.assertRaises(ValidationError):
            ArtifactRefV1.model_validate(
                {
                    "artifact_id": str(uuid4()),
                    "artifact_type": "QUERY",
                    "sha256": ZERO_SHA,
                    "schema_id": "query",
                    "schema_version": "1",
                }
            )
        with self.assertRaises(ValidationError):
            PolicySnapshotRefV1(
                policy_snapshot_id=uuid4(),
                policy_revision=1,
                policy_digest=ZERO_SHA,
                observed_at=datetime(2026, 9, 15),
            )
        ref = PolicySnapshotRefV1(
            policy_snapshot_id=uuid4(),
            policy_revision=1,
            policy_digest=ZERO_SHA,
            observed_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
        self.assertIs(ref.observed_at.tzinfo, UTC)

    def test_knowledge_scope_branches(self) -> None:
        KnowledgeScopeV1(
            mode=KnowledgeScopeModeV1.DEFAULT_AUTHORIZED,
            directory_ids=(),
            knowledge_version_ids=(),
            include_personal=False,
        )
        for value in (
            {
                "mode": "NONE",
                "directory_ids": (uuid4(),),
                "knowledge_version_ids": (),
                "include_personal": False,
            },
            {
                "mode": "EXPLICIT",
                "directory_ids": (),
                "knowledge_version_ids": (),
                "include_personal": False,
            },
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                KnowledgeScopeV1.model_validate(value)

    def test_output_contract_schema_relation(self) -> None:
        schema = SchemaRefV1(schema_id="answer-v1", schema_version="1", schema_digest=ZERO_SHA)
        OutputContractV1(
            format="JSON",
            delivery="INLINE",
            language="zh-CN",
            schema_ref=schema,
            template_or_skill_ref=None,
            citations="FORBIDDEN",
            max_output_bytes=1024,
        )
        with self.assertRaises(ValidationError):
            OutputContractV1(
                format="MARKDOWN",
                delivery="INLINE",
                language="zh-CN",
                schema_ref=schema,
                template_or_skill_ref=None,
                citations="FORBIDDEN",
                max_output_bytes=1024,
            )
        with self.assertRaises(ValidationError):
            OutputContractV1(
                format="MARKDOWN",
                delivery="INLINE",
                language="zh--CN",
                schema_ref=None,
                template_or_skill_ref=None,
                citations="FORBIDDEN",
                max_output_bytes=1024,
            )

    def test_snapshot_digest_uses_complete_normalized_object(self) -> None:
        task_id = uuid4()
        payload = {
            "contract_version": "task_input_snapshot_v1",
            "input_id": str(uuid4()),
            "task_id": str(task_id),
            "input_revision": 1,
            "message_id": str(uuid4()),
            "message_ref": {
                "artifact_id": str(uuid4()),
                "artifact_type": "USER_QUERY",
                "sha256": ZERO_SHA,
                "schema_id": "user-query",
                "schema_version": "1",
            },
            "actor_id": str(uuid4()),
            "organization_id": str(uuid4()),
            "conversation_id": str(uuid4()),
            "attachment_binding_refs": [],
            "structured_controls": {
                "knowledge_scope": {
                    "mode": "DEFAULT_AUTHORIZED",
                    "directory_ids": [],
                    "knowledge_version_ids": [],
                    "include_personal": False,
                },
                "network_policy": {"value": "ASK", "source": "SYSTEM_DEFAULT"},
                "output_contract": {
                    "format": "MARKDOWN",
                    "delivery": "INLINE",
                    "language": "zh-CN",
                    "schema_ref": None,
                    "template_or_skill_ref": None,
                    "citations": "OPTIONAL",
                    "max_output_bytes": 1024,
                },
                "selected_resource_refs": [],
            },
            "task_constraint_refs": [],
            "policy_snapshot_ref": {
                "policy_snapshot_id": str(uuid4()),
                "policy_revision": 1,
                "policy_digest": ZERO_SHA,
                "observed_at": "2026-09-15T00:00:00Z",
            },
            "locale": "zh-CN",
            "timezone": "Asia/Shanghai",
            "created_at": "2026-09-15T00:00:00Z",
        }
        payload["snapshot_digest"] = canonical_json_sha256(payload)
        TaskInputSnapshotV1.model_validate_json(json.dumps(payload))
        payload["input_revision"] = 2
        with self.assertRaises(ValidationError):
            TaskInputSnapshotV1.model_validate_json(json.dumps(payload))

    def test_create_task_utf8_and_attachment_relations(self) -> None:
        attachment_id = uuid4()
        base = {
            "conversation_id": uuid4(),
            "query": "给出下一步建议",
            "attachments": (
                CreateTaskAttachmentV1(artifact_id=attachment_id, sha256=ZERO_SHA, ordinal=1),
            ),
            "knowledge_scope": None,
            "output_contract": None,
            "network_policy": None,
            "selected_resource_refs": (),
            "explicit_constraints": (),
        }
        CreateTaskRequestV1.model_validate(base)
        for update in (
            {"query": "\ud800"},
            {"query": "\x00"},
            {
                "attachments": (
                    CreateTaskAttachmentV1(artifact_id=attachment_id, sha256=ZERO_SHA, ordinal=2),
                )
            },
        ):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                CreateTaskRequestV1.model_validate({**base, **update})

    def test_create_task_raw_body_and_digest(self) -> None:
        body = json.dumps(
            {
                "conversation_id": str(uuid4()),
                "query": "建议",
                "attachments": [],
                "knowledge_scope": None,
                "output_contract": None,
                "network_policy": None,
                "selected_resource_refs": [],
                "explicit_constraints": [],
            },
            ensure_ascii=False,
        ).encode()
        request = parse_create_task_request(body)
        compact = json.dumps(json.loads(body), ensure_ascii=False, separators=(",", ":")).encode()
        self.assertEqual(
            create_task_request_digest(request),
            create_task_request_digest(parse_create_task_request(compact)),
        )
        changed = compact.replace("建议".encode(), "建议二".encode())
        self.assertNotEqual(
            create_task_request_digest(request),
            create_task_request_digest(parse_create_task_request(changed)),
        )
        duplicate = body.replace(b'"query":', b'"query":"first","query":', 1)
        with self.assertRaisesRegex(ValueError, "DUPLICATE_JSON_KEY"):
            parse_create_task_request(duplicate)
        with self.assertRaisesRegex(ValueError, "CREATE_TASK_BODY_SIZE_LIMIT"):
            parse_create_task_request(b" " * 262_145)

    def test_default_controls_preserve_source(self) -> None:
        scope = normalize_knowledge_scope(None)
        self.assertIs(scope.mode, KnowledgeScopeModeV1.DEFAULT_AUTHORIZED)
        self.assertFalse(scope.include_personal)
        default_network = normalize_network_control(None)
        self.assertEqual(default_network.value, "ASK")
        self.assertIs(default_network.source, NetworkPolicySourceV1.SYSTEM_DEFAULT)
        explicit_network = normalize_network_control("ASK")
        self.assertIs(explicit_network.source, NetworkPolicySourceV1.UI_FIELD)

    def test_goal_has_no_authority_fields(self) -> None:
        base = {
            "contract_version": "supervisor_goal_understanding_v1",
            "input_id": uuid4(),
            "primary_goal": "get recommendation",
            "expected_deliverable": "next steps",
            "subgoals": (),
            "effective_constraint_refs": (),
            "resource_refs": (),
            "success_criteria_proposals": (
                {
                    "local_id": "goalcriterion1",
                    "statement": "recommendation produced",
                    "evidence_kind": "ARTIFACT",
                },
            ),
            "safe_assumptions": (),
            "blocking_issues": (),
        }
        GoalUnderstandingV1.model_validate(base)
        for field in ("confidence", "risk_level", "grant_id", "final_plan"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                GoalUnderstandingV1.model_validate({**base, field: "forged"})

    def test_goal_references_are_bounded_by_context(self) -> None:
        input_id = uuid4()
        query_ref = ArtifactRefV1(
            artifact_id=uuid4(),
            artifact_type="USER_QUERY",
            sha256=ZERO_SHA,
            schema_id="user-query",
            schema_version="1",
        )
        policy_ref = PolicySnapshotRefV1(
            policy_snapshot_id=uuid4(),
            policy_revision=1,
            policy_digest=ZERO_SHA,
            observed_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
        controls = {
            "knowledge_scope": {
                "mode": "DEFAULT_AUTHORIZED",
                "directory_ids": [],
                "knowledge_version_ids": [],
                "include_personal": False,
            },
            "network_policy": {"value": "ASK", "source": "SYSTEM_DEFAULT"},
            "output_contract": {
                "format": "MARKDOWN",
                "delivery": "INLINE",
                "language": "zh-CN",
                "schema_ref": None,
                "template_or_skill_ref": None,
                "citations": "OPTIONAL",
                "max_output_bytes": 1024,
            },
            "selected_resource_refs": [],
        }
        context = GoalUnderstandingContextV1.model_validate_json(
            json.dumps(
                {
                    "contract_version": "goal_understanding_context_v1",
                    "task_id": str(uuid4()),
                    "input_id": str(input_id),
                    "input_snapshot_digest": ZERO_SHA,
                    "query_ref": query_ref.model_dump(mode="json"),
                    "structured_controls": controls,
                    "effective_constraint_refs": [],
                    "authorized_resource_refs": [],
                    "classifier_annotation_ref": None,
                    "policy_snapshot_ref": policy_ref.model_dump(mode="json"),
                    "deadline_at": "2026-09-15T00:10:00Z",
                    "prompt_profile": "supervisor-goal-understanding-v1",
                    "schema_version": "supervisor_goal_understanding_v1",
                }
            )
        )
        goal = GoalUnderstandingV1.model_validate(
            {
                "contract_version": "supervisor_goal_understanding_v1",
                "input_id": uuid4(),
                "primary_goal": "recommend",
                "expected_deliverable": "next step",
                "subgoals": (),
                "effective_constraint_refs": (),
                "resource_refs": (),
                "success_criteria_proposals": (
                    {
                        "local_id": "goalcriterion1",
                        "statement": "recommendation exists",
                        "evidence_kind": "ARTIFACT",
                    },
                ),
                "safe_assumptions": (),
                "blocking_issues": (),
            }
        )
        reasons = {issue.reason for issue in validate_goal_relations(goal, context)}
        self.assertEqual(reasons, {GoalSemanticReasonV1.INPUT_MISMATCH})

    def test_goal_gate_requires_validated_issue(self) -> None:
        common = {
            "understanding_id": uuid4(),
            "understanding_digest": ZERO_SHA,
            "accepted_assumption_ids": (),
            "gate_profile_digest": ZERO_SHA,
        }
        GoalGateDecisionV1.model_validate(
            {
                **common,
                "outcome": "READY_TO_PLAN",
                "validated_issue_ids": (),
                "reason_codes": (),
            }
        )
        with self.assertRaises(ValidationError):
            GoalGateDecisionV1.model_validate(
                {
                    **common,
                    "outcome": "REQUEST_USER_INPUT",
                    "validated_issue_ids": (),
                    "reason_codes": (),
                }
            )


if __name__ == "__main__":
    unittest.main()
