"""DEV-06 ActionBinder 离线验收：严格 Schema 校验、RFC 6901 投影、冻结 Normalizer、三类 Digest。

不接真实模型/PG/Catalog；只证明确定性绑定器与规范事实契约的正确性。
"""

import unittest
from uuid import UUID

from pydantic import ValidationError

from knowledge_system.modules.tasking.domain.action_binding import (
    ActionBindingV1,
    ActionProjectionRuleV1,
    CapabilityActionProfileV1,
    CapabilityBindingSnapshotV1,
    CapabilityToolBindingV1,
    bind_action,
    project_pointer,
    validate_arguments_against_schema,
)
from knowledge_system.modules.tasking.domain.action_candidate import InlineJsonV1, ProposedActionV1
from knowledge_system.modules.tasking.domain.action_facts import (
    CanonicalNetworkTargetV1,
    CanonicalRecipientV1,
    CanonicalResourceRefV1,
    DataFlowFactV1,
    normalize,
)
from knowledge_system.modules.tasking.domain.core_types import (
    ArtifactRefV1,
    CapabilityRefV1,
    VersionedRefV1,
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


def versioned_ref(ref_id: UUID) -> VersionedRefV1:
    return VersionedRefV1(ref_id=ref_id, version=1, integrity_digest=ZERO_SHA)


def inline_payload(arguments: dict) -> InlineJsonV1:
    return InlineJsonV1(
        payload_kind="INLINE_JSON",
        inline_json=arguments,
        artifact_ref=None,
        serialized_bytes=64,
    )


ARGUMENTS = {
    "query": "internal evidence",
    "limit": 3,
    "target": {"scheme": "https", "host": "Example.COM", "port": 443},
}

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1},
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
        arguments_payload=inline_payload(ARGUMENTS),
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
        tool_policy_profile_ref=versioned_ref(UUID("55555555-5555-4555-8555-555555555555")),
        disclosure="EAGER",
        model_status="COMPATIBLE",
    )
    kwargs.update(overrides)
    return CapabilityToolBindingV1(**kwargs)


def snapshot(**overrides) -> CapabilityBindingSnapshotV1:
    kwargs = dict(
        contract_version="capability_toolset_binding_v1",
        snapshot_id=SNAPSHOT_ID,
        task_id=TASK_ID,
        task_attempt_id=ATTEMPT_ID,
        agent_manifest_ref=versioned_ref(UUID("44444444-4444-4444-8444-444444444444")),
        plan_version=1,
        parent_grant_refs=(),
        model_capability="claude-sonnet-5",
        provider_capabilities_digest=ZERO_SHA,
        bindings=(tool_binding(),),
        integrity_digest=ZERO_SHA,
    )
    kwargs.update(overrides)
    return CapabilityBindingSnapshotV1(**kwargs)


def profile(**overrides) -> CapabilityActionProfileV1:
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


def bind(**overrides):
    kwargs = dict(
        proposal=proposed_action(),
        snapshot=snapshot(),
        profile=profile(),
        input_schema=INPUT_SCHEMA,
        proposal_ref=artifact_ref(),
        purpose_code="evidence-search",
        sequence_no=1,
    )
    kwargs.update(overrides)
    return bind_action(**kwargs)


class ProjectionPointerTests(unittest.TestCase):
    def test_root_pointer_returns_document(self) -> None:
        doc = {"a": 1}
        self.assertEqual(project_pointer(doc, ""), doc)

    def test_object_path(self) -> None:
        self.assertEqual(project_pointer({"a": {"b": 2}}, "/a/b"), 2)

    def test_array_index(self) -> None:
        self.assertEqual(project_pointer({"xs": ["a", "b"]}, "/xs/1"), "b")

    def test_tilde_escapes(self) -> None:
        self.assertEqual(project_pointer({"a/b": 1, "m~n": 2}, "/a~1b"), 1)
        self.assertEqual(project_pointer({"m~n": 2}, "/m~0n"), 2)

    def test_missing_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            project_pointer({"a": 1}, "/missing")

    def test_missing_array_index_raises(self) -> None:
        with self.assertRaises(ValueError):
            project_pointer({"xs": ["a"]}, "/xs/3")

    def test_invalid_pointer_raises(self) -> None:
        with self.assertRaises(ValueError):
            project_pointer({"a": 1}, "a")

    def test_array_leading_zero_rejected(self) -> None:
        with self.assertRaises(ValueError):
            project_pointer({"xs": ["a", "b"]}, "/xs/01")


class StrictSchemaValidationTests(unittest.TestCase):
    def test_valid_arguments_pass(self) -> None:
        validate_arguments_against_schema(INPUT_SCHEMA, ARGUMENTS)

    def test_wrong_type_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_arguments_against_schema(INPUT_SCHEMA, {**ARGUMENTS, "limit": "3"})

    def test_missing_required_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_arguments_against_schema(INPUT_SCHEMA, {"query": "q"})

    def test_no_type_coercion(self) -> None:
        schema = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        }
        with self.assertRaises(ValueError):
            validate_arguments_against_schema(schema, {"count": "5"})

    def test_default_not_applied(self) -> None:
        schema = {
            "type": "object",
            "properties": {"x": {"type": "string", "default": "abc"}},
            "required": ["x"],
        }
        with self.assertRaises(ValueError):
            validate_arguments_against_schema(schema, {})

    def test_invalid_schema_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_arguments_against_schema({"required": "not-an-array"}, {})


class NormalizerTests(unittest.TestCase):
    def test_unknown_normalizer_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize("jsonpath_v1", "x")

    def test_opaque_string(self) -> None:
        self.assertEqual(normalize("opaque_string_v1", "abc"), "abc")
        with self.assertRaises(ValueError):
            normalize("opaque_string_v1", 5)
        with self.assertRaises(ValueError):
            normalize("opaque_string_v1", "")

    def test_resource_ref(self) -> None:
        ref = normalize(
            "resource_ref_v1",
            {"resource_kind": "ARTIFACT", "identifier": "doc-1", "integrity_digest": ZERO_SHA},
        )
        self.assertIsInstance(ref, CanonicalResourceRefV1)
        self.assertEqual(ref.identifier, "doc-1")
        with self.assertRaises(ValueError):
            normalize(
                "resource_ref_v1", {"resource_kind": "ARTIFACT", "identifier": "x", "extra": 1}
            )
        with self.assertRaises(ValueError):
            normalize("resource_ref_v1", {"resource_kind": "ARTIFACT"})

    def test_network_target_lowercases_host(self) -> None:
        target = normalize(
            "network_target_v1", {"scheme": "https", "host": "Example.COM", "port": 443}
        )
        self.assertEqual(target.host, "example.com")
        self.assertEqual(target.port, 443)
        with self.assertRaises(ValueError):
            normalize("network_target_v1", {"scheme": "ftp", "host": "x"})
        with self.assertRaises(ValueError):
            normalize("network_target_v1", {"scheme": "https", "host": "a/b"})

    def test_absolute_uri(self) -> None:
        self.assertEqual(normalize("absolute_uri_v1", "https://a.example/x"), "https://a.example/x")
        with self.assertRaises(ValueError):
            normalize("absolute_uri_v1", "a.example/x")

    def test_workspace_path(self) -> None:
        self.assertEqual(normalize("workspace_path_v1", "docs/report.md"), "docs/report.md")
        with self.assertRaises(ValueError):
            normalize("workspace_path_v1", "/etc/passwd")
        with self.assertRaises(ValueError):
            normalize("workspace_path_v1", "docs/../secret")
        with self.assertRaises(ValueError):
            normalize("workspace_path_v1", "docs\\secret")
        with self.assertRaises(ValueError):
            normalize("workspace_path_v1", "C:/secret")

    def test_recipient_set_sorts_and_dedups(self) -> None:
        recipients = normalize(
            "recipient_set_v1",
            [
                {"recipient_kind": "USER_ID", "identifier": "u2"},
                {"recipient_kind": "USER_ID", "identifier": "u1"},
                {"recipient_kind": "USER_ID", "identifier": "u1"},
            ],
        )
        self.assertEqual([r.identifier for r in recipients], ["u1", "u2"])
        self.assertIsInstance(recipients[0], CanonicalRecipientV1)
        with self.assertRaises(ValueError):
            normalize("recipient_set_v1", "not-a-list")

    def test_ordered_sequence_preserves_order(self) -> None:
        self.assertEqual(normalize("ordered_sequence_v1", [3, 1, 2]), (3, 1, 2))

    def test_identifier_set_sorts_and_dedups(self) -> None:
        self.assertEqual(normalize("identifier_set_v1", ["b", "a", "b"]), ("a", "b"))

    def test_utc_timestamp_requires_timezone(self) -> None:
        self.assertEqual(
            normalize("utc_timestamp_v1", "2026-10-08T04:00:00+08:00"), "2026-10-07T20:00:00Z"
        )
        with self.assertRaises(ValueError):
            normalize("utc_timestamp_v1", "2026-10-08T12:00:00")
        with self.assertRaises(ValueError):
            normalize("utc_timestamp_v1", "not-a-date")

    def test_byte_count(self) -> None:
        self.assertEqual(normalize("byte_count_v1", 10), 10)
        with self.assertRaises(ValueError):
            normalize("byte_count_v1", True)
        with self.assertRaises(ValueError):
            normalize("byte_count_v1", -1)
        with self.assertRaises(ValueError):
            normalize("byte_count_v1", 1.5)


class CanonicalFactModelTests(unittest.TestCase):
    def test_extra_field_forbidden(self) -> None:
        with self.assertRaises(ValidationError):
            CanonicalResourceRefV1.model_validate(
                {"resource_kind": "ARTIFACT", "identifier": "x", "forged": 1}
            )

    def test_unknown_resource_kind_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            CanonicalResourceRefV1.model_validate({"resource_kind": "SECRET", "identifier": "x"})


class BindingResolutionTests(unittest.TestCase):
    def test_snapshot_id_mismatch(self) -> None:
        other = snapshot(snapshot_id=UUID("99999999-9999-4999-8999-999999999999"))
        with self.assertRaises(ValueError):
            bind(snapshot=other)

    def test_snapshot_digest_mismatch(self) -> None:
        other = snapshot(integrity_digest="1" * 64)
        with self.assertRaises(ValueError):
            bind(snapshot=other)

    def test_tool_not_disclosed(self) -> None:
        other = snapshot(bindings=(tool_binding(model_tool_name="other_tool_v1"),))
        with self.assertRaises(ValueError):
            bind(snapshot=other)

    def test_profile_capability_mismatch(self) -> None:
        other_ref = CapabilityRefV1(
            capability_id="other", capability_version="1", integrity_digest=ZERO_SHA
        )
        with self.assertRaises(ValueError):
            bind(profile=profile(capability_ref=other_ref))

    def test_artifact_ref_payload_requires_resolution(self) -> None:
        payload = InlineJsonV1(
            payload_kind="ARTIFACT_REF",
            inline_json=None,
            artifact_ref=artifact_ref(),
            serialized_bytes=0,
        )
        with self.assertRaises(ValueError):
            bind(proposal=proposed_action(arguments_payload=payload))

    def test_sequence_no_invalid(self) -> None:
        with self.assertRaises(ValueError):
            bind(sequence_no=0)


class BindActionTests(unittest.TestCase):
    def test_happy_path_facts(self) -> None:
        binding = bind()
        self.assertIsInstance(binding, ActionBindingV1)
        facts = binding.canonical_facts
        self.assertEqual(
            facts.data_inputs,
            (DataFlowFactV1(payload_kind="OPAQUE", identifier="internal evidence"),),
        )
        self.assertEqual(
            facts.network_targets,
            (CanonicalNetworkTargetV1(scheme="https", host="example.com", port=443),),
        )
        self.assertEqual(facts.resources, ())
        self.assertEqual(facts.recipients, ())
        self.assertEqual(facts.data_egress, ())
        self.assertEqual(facts.action_family, "search")

    def test_digests_are_64_hex(self) -> None:
        binding = bind()
        for digest in (
            binding.arguments_digest,
            binding.semantic_action_digest,
            binding.execution_request_digest,
            binding.effect_fingerprint,
        ):
            self.assertEqual(len(digest), 64)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_binding_is_deterministic(self) -> None:
        self.assertEqual(bind(), bind())

    def test_schema_invalid_rejected(self) -> None:
        with self.assertRaises(ValueError):
            bind(proposal=proposed_action(arguments_payload=inline_payload({"query": "q"})))

    def test_required_pointer_missing_rejected(self) -> None:
        # /target 是 required 规则但参数缺省 -> 失败关闭。
        bad = proposed_action(arguments_payload=inline_payload({"query": "q"}))
        with self.assertRaises(ValueError):
            bind(proposal=bad)

    def test_optional_pointer_missing_skipped(self) -> None:
        p = profile(
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
                    source_pointer="/limit",
                    target_fact="COST_DRIVER",
                    normalizer_id="byte_count_v1",
                    required=False,
                ),
            )
        )
        binding = bind(
            profile=p,
            proposal=proposed_action(arguments_payload=inline_payload({"query": "q"})),
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )
        self.assertEqual(len(binding.canonical_facts.data_inputs), 1)

    def test_non_argument_source_kind_rejected(self) -> None:
        p = profile(
            projection_rules=(
                ActionProjectionRuleV1(
                    source_kind="PLAN_ITEM",
                    source_pointer="/item_id",
                    target_fact="DATA_INPUT",
                    normalizer_id="opaque_string_v1",
                    required=True,
                ),
            )
        )
        with self.assertRaises(ValueError):
            bind(profile=p)

    def test_semantic_digest_ignores_tool_name(self) -> None:
        p = profile(tool_id=None)
        first = bind(profile=p)
        other_tool = UUID("66666666-6666-4666-8666-666666666666")
        second = bind(
            profile=p,
            snapshot=snapshot(
                bindings=(tool_binding(tool_id=other_tool, model_tool_name="other_tool_v1"),)
            ),
            proposal=proposed_action(model_tool_name="other_tool_v1"),
        )
        self.assertEqual(first.semantic_action_digest, second.semantic_action_digest)
        self.assertNotEqual(first.execution_request_digest, second.execution_request_digest)

    def test_effect_fingerprint_excludes_tool_but_tracks_sequence(self) -> None:
        p = profile(tool_id=None)
        first = bind(profile=p)
        other_tool = UUID("66666666-6666-4666-8666-666666666666")
        second = bind(
            profile=p,
            snapshot=snapshot(
                bindings=(tool_binding(tool_id=other_tool, model_tool_name="other_tool_v1"),)
            ),
            proposal=proposed_action(model_tool_name="other_tool_v1"),
        )
        self.assertEqual(first.effect_fingerprint, second.effect_fingerprint)
        third = bind(sequence_no=2)
        self.assertNotEqual(first.effect_fingerprint, third.effect_fingerprint)

    def test_target_fact_normalizer_mismatch_rejected(self) -> None:
        p = profile(
            projection_rules=(
                ActionProjectionRuleV1(
                    source_kind="ARGUMENT",
                    source_pointer="/query",
                    target_fact="RESOURCE",
                    normalizer_id="opaque_string_v1",
                    required=True,
                ),
            )
        )
        with self.assertRaises(ValueError):
            bind(profile=p)


if __name__ == "__main__":
    unittest.main()
