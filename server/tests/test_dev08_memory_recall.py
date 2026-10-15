"""DEV-08 子批次 5：MemoryRecallService 确定性召回编排的离线盲测（假端口，零 LLM/零 I/O）。

覆盖 §9.2 确定性通道：canonical_key 精确匹配 + 最近 Active Memory、按 id 精确去重、
`revalidate_record` 闭包重验（owner/scope/state/有效期）、kind 过滤、冲突组兄弟展开、
确定性排序（importance 降序 → valid_from 降序 → id 升序）、`max_candidates` 截断与
`truncated` 标记、失败关闭的 `MemoryRecallRequestV1` 校验。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import ValidationError

from knowledge_system.modules.memory.public import (
    MemoryKindV1,
    MemoryRecallChannelV1,
    MemoryRecallRequestV1,
    MemoryRecallService,
    MemoryRecordV1,
    MemoryScopeV1,
    MemoryStateV1,
    MemoryTrustV1,
    ProvenanceRefV1,
    ProvenanceSourceTypeV1,
    revalidate_record,
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
        "canonical_key": None,
        "content": "使用 SI 单位",
        "canonical_hash": "b" * 64,
        "importance": 0.8,
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


def _request(**overrides: object) -> MemoryRecallRequestV1:
    defaults: dict[str, object] = {
        "owner_id": _OWNER,
        "scopes": (MemoryScopeV1.CONVERSATION,),
        "conversation_id": None,
        "task_id": None,
        "canonical_keys": (),
        "required_kinds": (),
        "max_candidates": 10,
        "now": _NOW,
    }
    defaults.update(overrides)
    return MemoryRecallRequestV1(**defaults)


class FakeMemoryRepository:
    """内存版 `MemoryRepositoryPort` 召回侧子集，按 owner+scope+ACTIVE 过滤。"""

    def __init__(self, records: tuple[MemoryRecordV1, ...] = ()) -> None:
        self._records: dict[UUID, MemoryRecordV1] = {record.id: record for record in records}
        self._by_key: dict[tuple[MemoryScopeV1, str], MemoryRecordV1] = {}
        for record in records:
            if record.canonical_key is not None:
                self._by_key[(record.scope, record.canonical_key)] = record

    async def find_active_by_key(
        self, *, owner_id: UUID, scope: MemoryScopeV1, canonical_key: str
    ) -> MemoryRecordV1 | None:
        record = self._by_key.get((scope, canonical_key))
        if (
            record is not None
            and record.owner_id == owner_id
            and record.state is MemoryStateV1.ACTIVE
        ):
            return record
        return None

    async def find_recent_active(
        self, *, owner_id: UUID, scope: MemoryScopeV1, limit: int
    ) -> tuple[MemoryRecordV1, ...]:
        active = [
            record
            for record in self._records.values()
            if record.owner_id == owner_id
            and record.scope is scope
            and record.state is MemoryStateV1.ACTIVE
        ]
        active.sort(
            key=lambda record: (
                -record.importance,
                -record.valid_from.timestamp(),
                str(record.id),
            )
        )
        return tuple(active[:limit])

    async def find_pinned(
        self, *, owner_id: UUID, scope: MemoryScopeV1, limit: int
    ) -> tuple[MemoryRecordV1, ...]:
        pinned = [
            record
            for record in self._records.values()
            if record.owner_id == owner_id
            and record.scope is scope
            and record.state is MemoryStateV1.ACTIVE
            and record.pinned
        ]
        pinned.sort(
            key=lambda record: (
                -record.importance,
                -record.valid_from.timestamp(),
                str(record.id),
            )
        )
        return tuple(pinned[:limit])

    async def find_active_by_conflict_group(
        self, *, owner_id: UUID, conflict_group_id: UUID
    ) -> tuple[MemoryRecordV1, ...]:
        return tuple(
            sorted(
                (
                    record
                    for record in self._records.values()
                    if record.owner_id == owner_id
                    and record.conflict_group_id == conflict_group_id
                    and record.state is MemoryStateV1.ACTIVE
                ),
                key=lambda record: str(record.id),
            )
        )


class RevalidateRecordTests(unittest.TestCase):
    def test_valid_record_passes(self) -> None:
        record = _record()
        self.assertTrue(
            revalidate_record(
                record, owner_id=_OWNER, scopes=frozenset({MemoryScopeV1.CONVERSATION}), now=_NOW
            )
        )

    def test_wrong_owner_rejected(self) -> None:
        record = _record(owner_id=_OTHER)
        self.assertFalse(
            revalidate_record(
                record, owner_id=_OWNER, scopes=frozenset({MemoryScopeV1.CONVERSATION}), now=_NOW
            )
        )

    def test_scope_not_in_allowed_rejected(self) -> None:
        record = _record(scope=MemoryScopeV1.TASK)
        self.assertFalse(
            revalidate_record(
                record, owner_id=_OWNER, scopes=frozenset({MemoryScopeV1.CONVERSATION}), now=_NOW
            )
        )

    def test_non_active_state_rejected(self) -> None:
        record = _record(state=MemoryStateV1.SUPERSEDED)
        self.assertFalse(
            revalidate_record(
                record, owner_id=_OWNER, scopes=frozenset({MemoryScopeV1.CONVERSATION}), now=_NOW
            )
        )

    def test_future_valid_from_rejected(self) -> None:
        record = _record(valid_from=_NOW + timedelta(hours=1))
        self.assertFalse(
            revalidate_record(
                record, owner_id=_OWNER, scopes=frozenset({MemoryScopeV1.CONVERSATION}), now=_NOW
            )
        )

    def test_expired_valid_until_rejected(self) -> None:
        record = _record(valid_until=_NOW - timedelta(seconds=1))
        self.assertFalse(
            revalidate_record(
                record, owner_id=_OWNER, scopes=frozenset({MemoryScopeV1.CONVERSATION}), now=_NOW
            )
        )

    def test_valid_until_still_open_passes(self) -> None:
        record = _record(valid_until=_NOW + timedelta(hours=1))
        self.assertTrue(
            revalidate_record(
                record, owner_id=_OWNER, scopes=frozenset({MemoryScopeV1.CONVERSATION}), now=_NOW
            )
        )


class RecallRequestContractTests(unittest.TestCase):
    def test_naive_now_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _request(now=datetime(2026, 10, 15, 12, 0, 0))

    def test_empty_scopes_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _request(scopes=())

    def test_zero_max_candidates_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _request(max_candidates=0)

    def test_extra_field_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            MemoryRecallRequestV1(**_request().model_dump(), bogus="x")


class MemoryRecallServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_key_channel(self) -> None:
        record = _record(canonical_key="output.language")
        result = await MemoryRecallService(FakeMemoryRepository((record,))).recall(
            _request(canonical_keys=("output.language",))
        )
        self.assertEqual([hit.record.id for hit in result.hits], [record.id])
        self.assertEqual(result.hits[0].channel, MemoryRecallChannelV1.EXACT_KEY)
        self.assertFalse(result.truncated)

    async def test_recent_active_channel(self) -> None:
        record = _record(canonical_key=None)
        result = await MemoryRecallService(FakeMemoryRepository((record,))).recall(_request())
        self.assertEqual([hit.record.id for hit in result.hits], [record.id])
        self.assertEqual(result.hits[0].channel, MemoryRecallChannelV1.RECENT_ACTIVE)

    async def test_pinned_channel(self) -> None:
        pinned = _record(canonical_key=None, pinned=True)
        result = await MemoryRecallService(FakeMemoryRepository((pinned,))).recall(_request())
        self.assertEqual([hit.record.id for hit in result.hits], [pinned.id])
        self.assertEqual(result.hits[0].channel, MemoryRecallChannelV1.PINNED)

    async def test_pinned_takes_channel_priority(self) -> None:
        record = _record(canonical_key="output.language", pinned=True)
        result = await MemoryRecallService(FakeMemoryRepository((record,))).recall(
            _request(canonical_keys=("output.language",))
        )
        self.assertEqual([hit.record.id for hit in result.hits], [record.id])
        self.assertEqual(result.hits[0].channel, MemoryRecallChannelV1.PINNED)

    async def test_dedup_across_channels_exact_key_wins(self) -> None:
        record = _record(canonical_key="output.language")
        repo = FakeMemoryRepository((record,))
        result = await MemoryRecallService(repo).recall(
            _request(canonical_keys=("output.language",))
        )
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.hits[0].channel, MemoryRecallChannelV1.EXACT_KEY)

    async def test_revalidation_rejects_expired_record(self) -> None:
        expired = _record(
            canonical_key=None,
            valid_from=_NOW - timedelta(hours=1),
            valid_until=_NOW - timedelta(seconds=1),
        )
        result = await MemoryRecallService(FakeMemoryRepository((expired,))).recall(_request())
        self.assertEqual(result.hits, ())
        self.assertEqual([record.id for record in result.rejected], [expired.id])

    async def test_kind_filter(self) -> None:
        pref = _record(id=uuid4(), kind=MemoryKindV1.PREFERENCE)
        goal = _record(id=uuid4(), kind=MemoryKindV1.GOAL)
        result = await MemoryRecallService(FakeMemoryRepository((pref, goal))).recall(
            _request(required_kinds=(MemoryKindV1.GOAL,))
        )
        self.assertEqual([hit.record.id for hit in result.hits], [goal.id])

    async def test_conflict_group_expands_both_sides(self) -> None:
        group = uuid4()
        key = "output.language"
        r1 = _record(id=uuid4(), canonical_key=key, conflict_group_id=group, importance=0.9)
        r2 = _record(id=uuid4(), canonical_key=key, conflict_group_id=group, importance=0.5)
        result = await MemoryRecallService(FakeMemoryRepository((r1, r2))).recall(
            _request(canonical_keys=(key,))
        )
        self.assertEqual({hit.record.id for hit in result.hits}, {r1.id, r2.id})

    async def test_deterministic_sort_by_importance(self) -> None:
        low = _record(id=uuid4(), importance=0.3, canonical_key=None)
        high = _record(id=uuid4(), importance=0.9, canonical_key=None)
        mid = _record(id=uuid4(), importance=0.6, canonical_key=None)
        result = await MemoryRecallService(FakeMemoryRepository((low, high, mid))).recall(
            _request()
        )
        self.assertEqual(
            [hit.record.id for hit in result.hits], [high.id, mid.id, low.id]
        )

    async def test_sort_tie_breaks_by_valid_from_then_id(self) -> None:
        older = _record(
            id=uuid4(), importance=0.8, valid_from=_NOW - timedelta(hours=1), canonical_key=None
        )
        newer = _record(id=uuid4(), importance=0.8, valid_from=_NOW, canonical_key=None)
        result = await MemoryRecallService(FakeMemoryRepository((older, newer))).recall(
            _request()
        )
        self.assertEqual([hit.record.id for hit in result.hits], [newer.id, older.id])

    async def test_max_candidates_caps_and_flags_truncation(self) -> None:
        first = _record(id=uuid4(), importance=0.9, canonical_key=None)
        second = _record(id=uuid4(), importance=0.8, canonical_key=None)
        result = await MemoryRecallService(FakeMemoryRepository((first, second))).recall(
            _request(max_candidates=1)
        )
        self.assertEqual([hit.record.id for hit in result.hits], [first.id])
        self.assertTrue(result.truncated)

    async def test_no_truncation_when_within_cap(self) -> None:
        record = _record(canonical_key=None)
        result = await MemoryRecallService(FakeMemoryRepository((record,))).recall(
            _request(max_candidates=5)
        )
        self.assertFalse(result.truncated)


if __name__ == "__main__":
    unittest.main()
