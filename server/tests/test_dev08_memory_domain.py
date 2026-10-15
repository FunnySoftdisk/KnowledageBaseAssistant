"""DEV-08 子批次 1：三作用域记忆领域契约与写入治理纯规则的离线盲测（零 LLM/零 I/O）。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import uuid4

from knowledge_system.modules.memory.domain.memory_governance import (
    assess_proposal_legality,
    clamp_importance,
    compute_canonical_hash,
    is_legal_state_transition,
    normalize_memory_statement,
    plan_correction_or_conflict,
    reject_sensitive_content,
    resolve_duplicate,
    should_bump_version,
)
from knowledge_system.modules.memory.domain.memory_types import (
    CorrectionDispositionV1,
    DuplicateVerdictV1,
    MemoryChangeKindV1,
    MemoryKindV1,
    MemoryProposalV1,
    MemoryRecordV1,
    MemoryScopeV1,
    MemoryStateV1,
    MemoryTrustV1,
    MemoryVersionSnapshotV1,
    ProvenanceRefV1,
    ProvenanceSourceTypeV1,
    freeze_memory_version_snapshot,
    memory_versions_digest_for,
)


def _provenance(**overrides: object) -> ProvenanceRefV1:
    defaults: dict[str, object] = {
        "source_type": ProvenanceSourceTypeV1.MESSAGE,
        "source_id": "m1",
        "content_hash": "a" * 64,
    }
    defaults.update(overrides)
    return ProvenanceRefV1(**defaults)


def _proposal(**overrides: object) -> MemoryProposalV1:
    defaults: dict[str, object] = {
        "scope": MemoryScopeV1.CONVERSATION,
        "kind": MemoryKindV1.PREFERENCE,
        "canonical_key": None,
        "statement": "使用 SI 单位",
        "importance": 0.8,
        "valid_until": None,
        "trust": MemoryTrustV1.USER_DERIVED,
        "provenance": (_provenance(),),
        "correction_of": None,
    }
    defaults.update(overrides)
    return MemoryProposalV1(**defaults)


def _record(**overrides: object) -> MemoryRecordV1:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "owner_id": uuid4(),
        "conversation_id": uuid4(),
        "task_id": None,
        "scope": MemoryScopeV1.CONVERSATION,
        "kind": MemoryKindV1.PREFERENCE,
        "state": MemoryStateV1.ACTIVE,
        "trust": MemoryTrustV1.USER_EXPLICIT,
        "canonical_key": "output.language",
        "content": "使用 SI 单位",
        "canonical_hash": "b" * 64,
        "importance": 1.0,
        "valid_from": datetime.now(UTC),
        "valid_until": None,
        "supersedes_id": None,
        "conflict_group_id": None,
        "provenance": (_provenance(),),
        "schema_version": 1,
        "revision": 1,
    }
    defaults.update(overrides)
    return MemoryRecordV1(**defaults)


class MemoryEnumContractTests(unittest.TestCase):
    def test_scope_has_exactly_three_values(self) -> None:
        self.assertEqual(
            set(MemoryScopeV1),
            {MemoryScopeV1.TASK, MemoryScopeV1.CONVERSATION, MemoryScopeV1.USER_PRIVATE},
        )
        self.assertNotIn("TEAM", {member.value for member in MemoryScopeV1})
        self.assertNotIn("SHARED", {member.value for member in MemoryScopeV1})

    def test_enum_value_roundtrip(self) -> None:
        self.assertEqual(MemoryKindV1.GOAL.value, "GOAL")
        self.assertEqual(MemoryStateV1.PROPOSED.value, "PROPOSED")
        self.assertEqual(MemoryTrustV1.USER_EXPLICIT.value, "USER_EXPLICIT")
        self.assertEqual(ProvenanceSourceTypeV1.EVIDENCE.value, "EVIDENCE")
        self.assertEqual(len(MemoryStateV1), 7)


class MemoryVersionSnapshotTests(unittest.TestCase):
    def test_defaults_to_zero(self) -> None:
        snap = MemoryVersionSnapshotV1()
        self.assertEqual(snap.conversation_version, 0)
        self.assertEqual(snap.user_private_version, 0)
        self.assertEqual(snap.task_version, 0)

    def test_digest_deterministic(self) -> None:
        first = memory_versions_digest_for(MemoryVersionSnapshotV1(conversation_version=2))
        second = memory_versions_digest_for(MemoryVersionSnapshotV1(conversation_version=2))
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_digest_field_order_sensitive(self) -> None:
        left = freeze_memory_version_snapshot(MemoryVersionSnapshotV1(conversation_version=1))
        right = freeze_memory_version_snapshot(MemoryVersionSnapshotV1(task_version=1))
        self.assertNotEqual(left.memory_versions_digest, right.memory_versions_digest)

    def test_freeze_fills_digest(self) -> None:
        frozen = freeze_memory_version_snapshot(MemoryVersionSnapshotV1())
        self.assertNotEqual(frozen.memory_versions_digest, "0" * 64)


class NormalizationTests(unittest.TestCase):
    def test_nfkc_and_casefold_full_width(self) -> None:
        self.assertEqual(normalize_memory_statement("ＡＢＣ １２３"), "abc 123")

    def test_collapses_whitespace(self) -> None:
        self.assertEqual(normalize_memory_statement("a   b\nc"), "a b c")

    def test_preserves_negation_number_unit(self) -> None:
        self.assertEqual(normalize_memory_statement("不 使用 5 kg"), "不 使用 5 kg")


class CanonicalHashTests(unittest.TestCase):
    def test_deterministic(self) -> None:
        first = compute_canonical_hash(
            MemoryScopeV1.CONVERSATION, MemoryKindV1.PREFERENCE, "k", "s"
        )
        second = compute_canonical_hash(
            MemoryScopeV1.CONVERSATION, MemoryKindV1.PREFERENCE, "k", "s"
        )
        self.assertEqual(first, second)

    def test_changes_with_each_field(self) -> None:
        base = compute_canonical_hash(MemoryScopeV1.CONVERSATION, MemoryKindV1.PREFERENCE, "k", "s")
        self.assertNotEqual(
            compute_canonical_hash(MemoryScopeV1.TASK, MemoryKindV1.PREFERENCE, "k", "s"), base
        )
        self.assertNotEqual(
            compute_canonical_hash(MemoryScopeV1.CONVERSATION, MemoryKindV1.GOAL, "k", "s"), base
        )
        self.assertNotEqual(
            compute_canonical_hash(MemoryScopeV1.CONVERSATION, MemoryKindV1.PREFERENCE, "k2", "s"),
            base,
        )
        self.assertNotEqual(
            compute_canonical_hash(MemoryScopeV1.CONVERSATION, MemoryKindV1.PREFERENCE, "k", "s2"),
            base,
        )

    def test_empty_key_equals_none_key(self) -> None:
        self.assertEqual(
            compute_canonical_hash(MemoryScopeV1.CONVERSATION, MemoryKindV1.PREFERENCE, None, "s"),
            compute_canonical_hash(MemoryScopeV1.CONVERSATION, MemoryKindV1.PREFERENCE, "", "s"),
        )


class ImportanceClampTests(unittest.TestCase):
    def test_explicit_remember_fixed_one(self) -> None:
        self.assertEqual(
            clamp_importance(
                MemoryTrustV1.USER_DERIVED,
                MemoryKindV1.PREFERENCE,
                explicit=True,
                proposed=0.3,
            ),
            (1.0, True),
        )

    def test_supported_finding_lower_bound(self) -> None:
        self.assertEqual(
            clamp_importance(
                MemoryTrustV1.EVIDENCE_SUPPORTED,
                MemoryKindV1.SUPPORTED_FINDING,
                explicit=False,
                proposed=0.2,
            ),
            (0.7, True),
        )

    def test_user_goal_lower_bound(self) -> None:
        self.assertEqual(
            clamp_importance(
                MemoryTrustV1.USER_DERIVED,
                MemoryKindV1.GOAL,
                explicit=False,
                proposed=0.5,
            ),
            (0.7, True),
        )

    def test_user_preference_range(self) -> None:
        self.assertEqual(
            clamp_importance(
                MemoryTrustV1.USER_DERIVED,
                MemoryKindV1.PREFERENCE,
                explicit=False,
                proposed=0.3,
            ),
            (0.5, True),
        )
        self.assertEqual(
            clamp_importance(
                MemoryTrustV1.USER_DERIVED,
                MemoryKindV1.PREFERENCE,
                explicit=False,
                proposed=0.8,
            ),
            (0.8, False),
        )

    def test_assistant_derived_caps_at_seven_tenths(self) -> None:
        self.assertEqual(
            clamp_importance(
                MemoryTrustV1.ASSISTANT_DERIVED,
                MemoryKindV1.PREFERENCE,
                explicit=False,
                proposed=0.9,
            ),
            (0.7, True),
        )

    def test_flag_false_when_within_range(self) -> None:
        self.assertEqual(
            clamp_importance(
                MemoryTrustV1.USER_DERIVED,
                MemoryKindV1.PREFERENCE,
                explicit=False,
                proposed=0.6,
            ),
            (0.6, False),
        )


class SensitiveContentTests(unittest.TestCase):
    def test_rejects_secrets(self) -> None:
        for statement in (
            "API key 是 sk-abc123",
            "password=secret123",
            "-----BEGIN " + "PRIVATE KEY-----",
            "Bearer abc.def",
            "这里包含私钥",
        ):
            self.assertTrue(reject_sensitive_content(statement), statement)

    def test_allows_benign(self) -> None:
        self.assertFalse(reject_sensitive_content("用户偏好使用 SI 单位"))


class ProposalLegalityTests(unittest.TestCase):
    def test_auto_conversation_legal(self) -> None:
        self.assertEqual(assess_proposal_legality(_proposal(), explicit=False), ())

    def test_auto_user_private_forbidden(self) -> None:
        violations = assess_proposal_legality(
            _proposal(scope=MemoryScopeV1.USER_PRIVATE), explicit=False
        )
        self.assertEqual([v.code for v in violations], ["MEMORY_SCOPE_PROMOTION_FORBIDDEN"])

    def test_explicit_user_private_legal(self) -> None:
        self.assertEqual(
            assess_proposal_legality(
                _proposal(scope=MemoryScopeV1.USER_PRIVATE), explicit=True
            ),
            (),
        )

    def test_assistant_derived_only_task(self) -> None:
        violations = assess_proposal_legality(
            _proposal(trust=MemoryTrustV1.ASSISTANT_DERIVED, scope=MemoryScopeV1.CONVERSATION),
            explicit=False,
        )
        self.assertEqual([v.code for v in violations], ["ASSISTANT_DERIVED_SCOPE_FORBIDDEN"])

    def test_supported_finding_requires_evidence(self) -> None:
        violations = assess_proposal_legality(
            _proposal(kind=MemoryKindV1.SUPPORTED_FINDING),
            explicit=False,
        )
        self.assertEqual(
            [v.code for v in violations], ["SUPPORTED_FINDING_REQUIRES_EVIDENCE"]
        )
        evidence = _provenance(source_type=ProvenanceSourceTypeV1.EVIDENCE, source_id="e1")
        legal = assess_proposal_legality(
            _proposal(kind=MemoryKindV1.SUPPORTED_FINDING, provenance=(evidence,)),
            explicit=False,
        )
        self.assertEqual(legal, ())


class DuplicateResolutionTests(unittest.TestCase):
    def test_same(self) -> None:
        resolution = resolve_duplicate(
            _proposal(canonical_key="k", statement="使用 SI 单位"),
            _record(canonical_key="k", content="使用 SI 单位"),
        )
        self.assertEqual(resolution.verdict, DuplicateVerdictV1.SAME)
        self.assertEqual(resolution.differing_fields, ())

    def test_overlap(self) -> None:
        resolution = resolve_duplicate(
            _proposal(canonical_key="k", statement="使用 SI 单位 且 时间 用 UTC"),
            _record(canonical_key="k", content="使用 SI 单位"),
        )
        self.assertEqual(resolution.verdict, DuplicateVerdictV1.OVERLAP)

    def test_conflict(self) -> None:
        resolution = resolve_duplicate(
            _proposal(canonical_key="k", statement="使用 公制 单位"),
            _record(canonical_key="k", content="使用 SI 单位"),
        )
        self.assertEqual(resolution.verdict, DuplicateVerdictV1.CONFLICT)
        self.assertIn("statement", resolution.differing_fields)

    def test_unrelated_without_key(self) -> None:
        resolution = resolve_duplicate(
            _proposal(canonical_key=None),
            _record(canonical_key="k"),
        )
        self.assertEqual(resolution.verdict, DuplicateVerdictV1.UNRELATED)

    def test_negation_recorded_as_differing_field(self) -> None:
        resolution = resolve_duplicate(
            _proposal(canonical_key="k", statement="不用 SI 单位"),
            _record(canonical_key="k", content="使用 SI 单位"),
        )
        self.assertEqual(resolution.verdict, DuplicateVerdictV1.CONFLICT)
        self.assertIn("negation", resolution.differing_fields)


class CorrectionPlanTests(unittest.TestCase):
    def test_new_when_no_relation(self) -> None:
        plan = plan_correction_or_conflict(
            _proposal(canonical_key=None), _record(canonical_key="k")
        )
        self.assertEqual(plan.disposition, CorrectionDispositionV1.NEW)

    def test_merge_same_key_same_value(self) -> None:
        plan = plan_correction_or_conflict(
            _proposal(canonical_key="k", statement="使用 SI 单位"),
            _record(canonical_key="k", content="使用 SI 单位"),
        )
        self.assertEqual(plan.disposition, CorrectionDispositionV1.MERGE)
        self.assertTrue(plan.append_provenance)

    def test_conflict_same_key_diff_value(self) -> None:
        plan = plan_correction_or_conflict(
            _proposal(canonical_key="k", statement="使用 公制 单位"),
            _record(canonical_key="k", content="使用 SI 单位"),
        )
        self.assertEqual(plan.disposition, CorrectionDispositionV1.CONFLICT)

    def test_correct_explicit_correction(self) -> None:
        existing = _record()
        plan = plan_correction_or_conflict(
            _proposal(correction_of=existing.id, trust=MemoryTrustV1.USER_EXPLICIT),
            existing,
        )
        self.assertEqual(plan.disposition, CorrectionDispositionV1.CORRECT)
        self.assertEqual(plan.supersedes_id, existing.id)

    def test_assistant_cannot_override_user(self) -> None:
        existing = _record(trust=MemoryTrustV1.USER_EXPLICIT)
        plan = plan_correction_or_conflict(
            _proposal(correction_of=existing.id, trust=MemoryTrustV1.ASSISTANT_DERIVED),
            existing,
        )
        self.assertEqual(plan.disposition, CorrectionDispositionV1.CONFLICT)
        self.assertIsNone(plan.supersedes_id)


class StateTransitionTests(unittest.TestCase):
    def test_legal_transitions(self) -> None:
        self.assertTrue(is_legal_state_transition(MemoryStateV1.PROPOSED, MemoryStateV1.ACTIVE))
        self.assertTrue(is_legal_state_transition(MemoryStateV1.ACTIVE, MemoryStateV1.SUPERSEDED))
        self.assertTrue(
            is_legal_state_transition(MemoryStateV1.QUARANTINED, MemoryStateV1.RETRACTED)
        )

    def test_illegal_and_terminal(self) -> None:
        self.assertFalse(is_legal_state_transition(MemoryStateV1.ACTIVE, MemoryStateV1.ACTIVE))
        self.assertFalse(is_legal_state_transition(MemoryStateV1.PROPOSED, MemoryStateV1.RETRACTED))
        self.assertFalse(is_legal_state_transition(MemoryStateV1.SUPERSEDED, MemoryStateV1.ACTIVE))
        self.assertFalse(is_legal_state_transition(MemoryStateV1.RETRACTED, MemoryStateV1.ACTIVE))


class VersionBumpTests(unittest.TestCase):
    def test_bumps_on_recallable_view_change(self) -> None:
        for kind in (
            MemoryChangeKindV1.ACTIVATED,
            MemoryChangeKindV1.CORRECTED,
            MemoryChangeKindV1.FORGOTTEN,
            MemoryChangeKindV1.INVALIDATED,
        ):
            self.assertTrue(should_bump_version(kind), kind)

    def test_no_bump_on_noop(self) -> None:
        for kind in (
            MemoryChangeKindV1.IDEMPOTENT_REPLAY,
            MemoryChangeKindV1.PROPOSED_ONLY,
            MemoryChangeKindV1.VALIDATION_REJECTED,
            MemoryChangeKindV1.EMBEDDING_REBUILD,
        ):
            self.assertFalse(should_bump_version(kind), kind)


if __name__ == "__main__":
    unittest.main()
