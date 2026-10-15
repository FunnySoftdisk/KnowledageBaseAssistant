"""DEV-08 子批次 2：MemoryWriteService 确定性写入编排的离线盲测（假端口，零 LLM/零 I/O）。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from knowledge_system.modules.memory.application.errors import MemoryError
from knowledge_system.modules.memory.application.memory_write_service import (
    MemoryWriteOutcomeV1,
    MemoryWriteService,
)
from knowledge_system.modules.memory.domain.memory_governance import (
    compute_canonical_hash,
    normalize_memory_statement,
)
from knowledge_system.modules.memory.domain.memory_types import (
    CorrectionDispositionV1,
    MemoryKindV1,
    MemoryProposalV1,
    MemoryRecordV1,
    MemoryScopeV1,
    MemoryStateV1,
    MemoryTrustV1,
    ProvenanceRefV1,
    ProvenanceSourceTypeV1,
)

_NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
_OWNER = uuid4()
_OTHER = uuid4()


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
        "owner_id": _OWNER,
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
        "valid_from": _NOW,
        "valid_until": None,
        "supersedes_id": None,
        "conflict_group_id": None,
        "provenance": (_provenance(),),
        "schema_version": 1,
        "revision": 1,
    }
    defaults.update(overrides)
    return MemoryRecordV1(**defaults)


def _hash(scope: MemoryScopeV1, kind: MemoryKindV1, key: str | None, statement: str) -> str:
    return compute_canonical_hash(scope, kind, key, normalize_memory_statement(statement))


class FakeMemoryRepository:
    """内存版 `MemoryRepositoryPort`，记录 commit/append 调用供断言。"""

    def __init__(self, records: tuple[MemoryRecordV1, ...] = ()) -> None:
        self._records: dict = {}
        self._by_hash: dict = {}
        self._by_key: dict = {}
        self.committed: list[MemoryRecordV1] = []
        self.appended: list[MemoryRecordV1] = []
        for record in records:
            self._seed(record)

    def _seed(self, record: MemoryRecordV1) -> None:
        self._records[record.id] = record
        self._by_hash[(record.scope, record.canonical_hash)] = record
        if record.canonical_key is not None:
            self._by_key[(record.scope, record.canonical_key)] = record

    async def find_active_by_hash(
        self, *, owner_id: object, scope: MemoryScopeV1, canonical_hash: str
    ) -> MemoryRecordV1 | None:
        record = self._by_hash.get((scope, canonical_hash))
        if (
            record is not None
            and record.state is MemoryStateV1.ACTIVE
            and record.owner_id == owner_id
        ):
            return record
        return None

    async def find_active_by_key(
        self, *, owner_id: object, scope: MemoryScopeV1, canonical_key: str
    ) -> MemoryRecordV1 | None:
        record = self._by_key.get((scope, canonical_key))
        if (
            record is not None
            and record.state is MemoryStateV1.ACTIVE
            and record.owner_id == owner_id
        ):
            return record
        return None

    async def commit_candidate(self, candidate: object) -> MemoryRecordV1:
        record = MemoryRecordV1(
            id=uuid4(),
            owner_id=candidate.owner_id,
            conversation_id=candidate.conversation_id,
            task_id=candidate.task_id,
            scope=candidate.scope,
            kind=candidate.kind,
            state=candidate.state,
            trust=candidate.trust,
            canonical_key=candidate.canonical_key,
            content=candidate.content,
            canonical_hash=candidate.canonical_hash,
            importance=candidate.importance,
            valid_from=candidate.valid_from,
            valid_until=candidate.valid_until,
            supersedes_id=candidate.supersedes_id,
            conflict_group_id=candidate.conflict_group_id,
            provenance=candidate.provenance,
            schema_version=1,
            revision=1,
        )
        self._seed(record)
        self.committed.append(record)
        return record

    async def append_provenance(
        self, record_id: object, provenance: tuple[ProvenanceRefV1, ...]
    ) -> MemoryRecordV1:
        record = self._records[record_id]
        merged = record.model_copy(
            update={"provenance": record.provenance + tuple(provenance)}
        )
        self._records[record_id] = merged
        self.appended.append(merged)
        return merged

    async def revise(self, command: object) -> MemoryRecordV1:
        raise NotImplementedError

    async def forget(self, command: object) -> object:
        raise NotImplementedError


class MemoryWriteServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_user_private_rejected(self) -> None:
        repo = FakeMemoryRepository()
        result = await MemoryWriteService(repo).validate_and_commit(
            _proposal(scope=MemoryScopeV1.USER_PRIVATE),
            owner_id=_OWNER,
            conversation_id=None,
            task_id=None,
            explicit=False,
            now=_NOW,
        )
        self.assertEqual(result.outcome, MemoryWriteOutcomeV1.REJECTED)
        self.assertIsNone(result.record)
        self.assertEqual(
            [v.code for v in result.violations], ["MEMORY_SCOPE_PROMOTION_FORBIDDEN"]
        )
        self.assertEqual(repo.committed, [])

    async def test_sensitive_content_rejected(self) -> None:
        repo = FakeMemoryRepository()
        result = await MemoryWriteService(repo).validate_and_commit(
            _proposal(statement="password=secret123"),
            owner_id=_OWNER,
            conversation_id=None,
            task_id=None,
            explicit=False,
            now=_NOW,
        )
        self.assertEqual(result.outcome, MemoryWriteOutcomeV1.REJECTED)
        self.assertEqual([v.code for v in result.violations], ["SENSITIVE_CONTENT_REJECTED"])
        self.assertEqual(repo.committed, [])

    async def test_explicit_remember_activated(self) -> None:
        repo = FakeMemoryRepository()
        result = await MemoryWriteService(repo).validate_and_commit(
            _proposal(importance=0.3),
            owner_id=_OWNER,
            conversation_id=None,
            task_id=None,
            explicit=True,
            now=_NOW,
        )
        self.assertEqual(result.outcome, MemoryWriteOutcomeV1.ACTIVATED)
        self.assertTrue(result.importance_clamped)
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.state, MemoryStateV1.ACTIVE)
        self.assertEqual(result.record.importance, 1.0)
        self.assertEqual(len(repo.committed), 1)

    async def test_user_derived_activated_within_range(self) -> None:
        repo = FakeMemoryRepository()
        result = await MemoryWriteService(repo).validate_and_commit(
            _proposal(importance=0.8),
            owner_id=_OWNER,
            conversation_id=None,
            task_id=None,
            explicit=False,
            now=_NOW,
        )
        self.assertEqual(result.outcome, MemoryWriteOutcomeV1.ACTIVATED)
        self.assertFalse(result.importance_clamped)
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.importance, 0.8)

    async def test_assistant_derived_quarantined(self) -> None:
        repo = FakeMemoryRepository()
        result = await MemoryWriteService(repo).validate_and_commit(
            _proposal(
                scope=MemoryScopeV1.TASK, trust=MemoryTrustV1.ASSISTANT_DERIVED, importance=0.9
            ),
            owner_id=_OWNER,
            conversation_id=None,
            task_id=uuid4(),
            explicit=False,
            now=_NOW,
        )
        self.assertEqual(result.outcome, MemoryWriteOutcomeV1.QUARANTINED)
        self.assertTrue(result.importance_clamped)
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.state, MemoryStateV1.QUARANTINED)
        self.assertEqual(result.record.importance, 0.7)

    async def test_evidence_supported_finding_activated(self) -> None:
        evidence = _provenance(source_type=ProvenanceSourceTypeV1.EVIDENCE, source_id="e1")
        repo = FakeMemoryRepository()
        result = await MemoryWriteService(repo).validate_and_commit(
            _proposal(
                kind=MemoryKindV1.SUPPORTED_FINDING,
                trust=MemoryTrustV1.EVIDENCE_SUPPORTED,
                provenance=(evidence,),
                importance=0.6,
            ),
            owner_id=_OWNER,
            conversation_id=None,
            task_id=None,
            explicit=False,
            now=_NOW,
        )
        self.assertEqual(result.outcome, MemoryWriteOutcomeV1.ACTIVATED)
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.state, MemoryStateV1.ACTIVE)
        self.assertEqual(result.record.importance, 0.7)

    async def test_exact_dedup_merged(self) -> None:
        key = "output.language"
        existing = _record(
            canonical_key=key,
            content="使用 SI 单位",
            canonical_hash=_hash(
                MemoryScopeV1.CONVERSATION, MemoryKindV1.PREFERENCE, key, "使用 SI 单位"
            ),
        )
        repo = FakeMemoryRepository((existing,))
        result = await MemoryWriteService(repo).validate_and_commit(
            _proposal(canonical_key=key, statement="使用 SI 单位"),
            owner_id=_OWNER,
            conversation_id=existing.conversation_id,
            task_id=None,
            explicit=False,
            now=_NOW,
        )
        self.assertEqual(result.outcome, MemoryWriteOutcomeV1.MERGED)
        self.assertEqual(result.disposition, CorrectionDispositionV1.MERGE)
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.id, existing.id)
        self.assertEqual(len(result.record.provenance), 2)
        self.assertEqual(repo.committed, [])
        self.assertEqual(len(repo.appended), 1)

    async def test_key_value_merge_different_kind(self) -> None:
        key = "output.language"
        existing = _record(
            canonical_key=key,
            kind=MemoryKindV1.PREFERENCE,
            content="使用 SI 单位",
            canonical_hash=_hash(
                MemoryScopeV1.CONVERSATION, MemoryKindV1.PREFERENCE, key, "使用 SI 单位"
            ),
        )
        repo = FakeMemoryRepository((existing,))
        result = await MemoryWriteService(repo).validate_and_commit(
            _proposal(canonical_key=key, kind=MemoryKindV1.CONSTRAINT, statement="使用 SI 单位"),
            owner_id=_OWNER,
            conversation_id=None,
            task_id=None,
            explicit=False,
            now=_NOW,
        )
        self.assertEqual(result.outcome, MemoryWriteOutcomeV1.MERGED)
        self.assertEqual(result.record.id, existing.id)
        self.assertEqual(len(result.record.provenance), 2)

    async def test_conflict_reuses_group_id(self) -> None:
        key = "output.language"
        group = uuid4()
        existing = _record(
            canonical_key=key,
            content="使用 SI 单位",
            conflict_group_id=group,
            canonical_hash=_hash(
                MemoryScopeV1.CONVERSATION, MemoryKindV1.PREFERENCE, key, "使用 SI 单位"
            ),
        )
        repo = FakeMemoryRepository((existing,))
        result = await MemoryWriteService(repo).validate_and_commit(
            _proposal(canonical_key=key, statement="使用 公制 单位"),
            owner_id=_OWNER,
            conversation_id=None,
            task_id=None,
            explicit=False,
            now=_NOW,
        )
        self.assertEqual(result.outcome, MemoryWriteOutcomeV1.ACTIVATED)
        self.assertEqual(result.disposition, CorrectionDispositionV1.CONFLICT)
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.state, MemoryStateV1.ACTIVE)
        self.assertEqual(result.record.conflict_group_id, group)

    async def test_conflict_allocates_fresh_group(self) -> None:
        key = "output.language"
        existing = _record(
            canonical_key=key,
            content="使用 SI 单位",
            conflict_group_id=None,
            canonical_hash=_hash(
                MemoryScopeV1.CONVERSATION, MemoryKindV1.PREFERENCE, key, "使用 SI 单位"
            ),
        )
        repo = FakeMemoryRepository((existing,))
        result = await MemoryWriteService(repo).validate_and_commit(
            _proposal(canonical_key=key, statement="使用 公制 单位"),
            owner_id=_OWNER,
            conversation_id=None,
            task_id=None,
            explicit=False,
            now=_NOW,
        )
        self.assertEqual(result.disposition, CorrectionDispositionV1.CONFLICT)
        self.assertIsNotNone(result.record)
        self.assertIsNotNone(result.record.conflict_group_id)

    async def test_correct_supersedes(self) -> None:
        key = "output.language"
        existing = _record(
            canonical_key=key,
            content="使用 SI 单位",
            trust=MemoryTrustV1.USER_EXPLICIT,
            canonical_hash=_hash(
                MemoryScopeV1.CONVERSATION, MemoryKindV1.PREFERENCE, key, "使用 SI 单位"
            ),
        )
        repo = FakeMemoryRepository((existing,))
        result = await MemoryWriteService(repo).validate_and_commit(
            _proposal(
                canonical_key=key,
                statement="使用 公制 单位",
                trust=MemoryTrustV1.USER_EXPLICIT,
                correction_of=existing.id,
            ),
            owner_id=_OWNER,
            conversation_id=None,
            task_id=None,
            explicit=False,
            now=_NOW,
        )
        self.assertEqual(result.disposition, CorrectionDispositionV1.CORRECT)
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.state, MemoryStateV1.ACTIVE)
        self.assertEqual(result.record.supersedes_id, existing.id)

    async def test_valid_until_in_past_rejected(self) -> None:
        repo = FakeMemoryRepository()
        result = await MemoryWriteService(repo).validate_and_commit(
            _proposal(valid_until=_NOW - timedelta(hours=1)),
            owner_id=_OWNER,
            conversation_id=None,
            task_id=None,
            explicit=False,
            now=_NOW,
        )
        self.assertEqual(result.outcome, MemoryWriteOutcomeV1.REJECTED)
        self.assertEqual([v.code for v in result.violations], ["MEMORY_VALID_UNTIL_IN_PAST"])
        self.assertEqual(repo.committed, [])

    async def test_preference_importance_clamped(self) -> None:
        repo = FakeMemoryRepository()
        result = await MemoryWriteService(repo).validate_and_commit(
            _proposal(importance=0.3),
            owner_id=_OWNER,
            conversation_id=None,
            task_id=None,
            explicit=False,
            now=_NOW,
        )
        self.assertTrue(result.importance_clamped)
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.importance, 0.5)

    async def test_naive_now_raises(self) -> None:
        repo = FakeMemoryRepository()
        with self.assertRaises(MemoryError) as ctx:
            await MemoryWriteService(repo).validate_and_commit(
                _proposal(),
                owner_id=_OWNER,
                conversation_id=None,
                task_id=None,
                explicit=False,
                now=datetime(2026, 10, 15, 12, 0, 0),
            )
        self.assertEqual(ctx.exception.code, "UTC_DATETIME_REQUIRED")

    async def test_owner_isolation_no_merge(self) -> None:
        key = "output.language"
        foreign = _record(
            owner_id=_OTHER,
            canonical_key=key,
            content="使用 SI 单位",
            canonical_hash=_hash(
                MemoryScopeV1.CONVERSATION, MemoryKindV1.PREFERENCE, key, "使用 SI 单位"
            ),
        )
        repo = FakeMemoryRepository((foreign,))
        result = await MemoryWriteService(repo).validate_and_commit(
            _proposal(canonical_key=key, statement="使用 SI 单位"),
            owner_id=_OWNER,
            conversation_id=None,
            task_id=None,
            explicit=False,
            now=_NOW,
        )
        self.assertEqual(result.outcome, MemoryWriteOutcomeV1.ACTIVATED)
        self.assertEqual(len(repo.committed), 1)


if __name__ == "__main__":
    unittest.main()
