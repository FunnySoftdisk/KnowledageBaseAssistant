"""DEV-08 子批次 3：`memory` Schema 权威 Store 的离线映射与真实 PG 验收。

离线部分证明 `record_from_candidate`/`memory_from_record` 互转确定性、Provenance JSON 往返、
坏 JSON 失败关闭；真实 PG 部分（`KNOWLEDGE_TEST_DATABASE_URL` 未配置时跳过）验证迁移 0012
建表、commit/find/append/revise/forget 全生命周期、owner 隔离与 QUARANTINED 状态过滤。
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from dev06_pg_support import TEST_DATABASE_URL, IsolatedDatabase
from sqlalchemy import select, text

from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.memory_models import (
    MemoryForgetTombstoneRecord,
)
from knowledge_system.infrastructure.persistence.memory_repository import (
    MemoryRecordIntegrityError,
    SqlAlchemyMemoryRepository,
    memory_from_record,
    record_from_candidate,
)
from knowledge_system.modules.memory.application.ports import (
    GovernedMemoryCandidateV1,
    MemoryForgetCommandV1,
    MemoryRevisionCommandV1,
)
from knowledge_system.modules.memory.domain.memory_governance import (
    compute_canonical_hash,
    normalize_memory_statement,
)
from knowledge_system.modules.memory.domain.memory_types import (
    MemoryKindV1,
    MemoryScopeV1,
    MemoryStateV1,
    MemoryTrustV1,
    ProvenanceRefV1,
    ProvenanceSourceTypeV1,
)

_NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
_ORG = uuid4()
_OWNER = uuid4()
_OTHER = uuid4()


def _hash(scope: MemoryScopeV1, kind: MemoryKindV1, key: str | None, statement: str) -> str:
    return compute_canonical_hash(scope, kind, key, normalize_memory_statement(statement))


def _provenance(**overrides: object) -> ProvenanceRefV1:
    defaults: dict[str, object] = {
        "source_type": ProvenanceSourceTypeV1.MESSAGE,
        "source_id": "m1",
        "source_version": None,
        "content_hash": "a" * 64,
        "message_sequence": 1,
    }
    defaults.update(overrides)
    return ProvenanceRefV1(**defaults)


def _candidate(**overrides: object) -> GovernedMemoryCandidateV1:
    defaults: dict[str, object] = {
        "owner_id": _OWNER,
        "conversation_id": None,
        "task_id": None,
        "scope": MemoryScopeV1.CONVERSATION,
        "kind": MemoryKindV1.PREFERENCE,
        "state": MemoryStateV1.ACTIVE,
        "trust": MemoryTrustV1.USER_EXPLICIT,
        "canonical_key": "output.language",
        "content": "使用 SI 单位",
        "canonical_hash": _hash(
            MemoryScopeV1.CONVERSATION, MemoryKindV1.PREFERENCE, "output.language", "使用 SI 单位"
        ),
        "importance": 1.0,
        "valid_from": _NOW,
        "valid_until": None,
        "supersedes_id": None,
        "conflict_group_id": None,
        "provenance": (_provenance(),),
    }
    defaults.update(overrides)
    return GovernedMemoryCandidateV1(**defaults)


class MemoryRecordMappingTests(unittest.TestCase):
    def test_candidate_record_roundtrip(self) -> None:
        candidate = _candidate()
        record_id = uuid4()
        orm = record_from_candidate(candidate, organization_id=_ORG, record_id=record_id)
        domain = memory_from_record(orm)
        self.assertEqual(domain.id, record_id)
        self.assertEqual(domain.owner_id, _OWNER)
        self.assertEqual(domain.scope, MemoryScopeV1.CONVERSATION)
        self.assertEqual(domain.kind, MemoryKindV1.PREFERENCE)
        self.assertEqual(domain.state, MemoryStateV1.ACTIVE)
        self.assertEqual(domain.trust, MemoryTrustV1.USER_EXPLICIT)
        self.assertEqual(domain.canonical_key, "output.language")
        self.assertEqual(domain.content, "使用 SI 单位")
        self.assertEqual(domain.canonical_hash, candidate.canonical_hash)
        self.assertEqual(domain.importance, 1.0)
        self.assertEqual(domain.valid_from, _NOW)
        self.assertIsNone(domain.valid_until)
        self.assertIsNone(domain.supersedes_id)
        self.assertIsNone(domain.conflict_group_id)
        self.assertEqual(domain.schema_version, 1)
        self.assertEqual(domain.revision, 1)
        self.assertEqual(len(domain.provenance), 1)
        self.assertEqual(domain.provenance[0].source_type, ProvenanceSourceTypeV1.MESSAGE)
        self.assertEqual(domain.provenance[0].content_hash, "a" * 64)
        self.assertEqual(domain.provenance[0].message_sequence, 1)

    def test_provenance_null_optionals_roundtrip(self) -> None:
        candidate = _candidate(
            provenance=(_provenance(source_version=None, message_sequence=None),)
        )
        orm = record_from_candidate(candidate, organization_id=_ORG, record_id=uuid4())
        domain = memory_from_record(orm)
        self.assertIsNone(domain.provenance[0].source_version)
        self.assertIsNone(domain.provenance[0].message_sequence)

    def test_memory_from_record_rejects_non_list_provenance(self) -> None:
        orm = record_from_candidate(_candidate(), organization_id=_ORG, record_id=uuid4())
        orm.provenance_json = "oops"  # type: ignore[assignment]
        with self.assertRaises(MemoryRecordIntegrityError):
            memory_from_record(orm)

    def test_memory_from_record_rejects_non_object_item(self) -> None:
        orm = record_from_candidate(_candidate(), organization_id=_ORG, record_id=uuid4())
        orm.provenance_json = ["not-an-object"]  # type: ignore[assignment]
        with self.assertRaises(MemoryRecordIntegrityError):
            memory_from_record(orm)


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class SqlAlchemyMemoryRepositoryPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev08_memory_{os.getpid()}_{uuid4().hex[:8]}"
        self.isolated = IsolatedDatabase(TEST_DATABASE_URL, self.dbname)
        self.url = await self.isolated.__aenter__()
        self.engine = build_async_engine(DatabaseEngineSettings(url=self.url))
        self.session_factory = build_session_factory(self.engine)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        await self.isolated.__aexit__(None, None, None)

    async def _seed_user(self, user_id: object, org_id: object) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO iam.organization (id,name,status,version) "
                    "VALUES (:id,:name,'ACTIVE',1) ON CONFLICT (id) DO NOTHING"
                ),
                {"id": org_id, "name": f"org-{org_id}"},
            )
            username = f"u-{user_id}"
            await conn.execute(
                text(
                    "INSERT INTO iam.user_account "
                    "(id,organization_id,username,normalized_username,display_name,"
                    "password_hash,status,must_change_password,failed_login_count,"
                    "credential_version,version) VALUES "
                    "(:id,:org,:u,:u,:u,'hash','ACTIVE',false,0,1,1)"
                ),
                {"id": user_id, "org": org_id, "u": username},
            )

    async def _tombstones(self, record_id: object) -> int:
        async with self.session_factory() as session:
            result = await session.scalars(
                select(MemoryForgetTombstoneRecord).where(
                    MemoryForgetTombstoneRecord.record_id == record_id
                )
            )
            return len(list(result))

    async def test_commit_and_find_by_hash(self) -> None:
        org, user = uuid4(), uuid4()
        await self._seed_user(user, org)
        candidate = _candidate(owner_id=user)
        async with self.session_factory() as session:
            committed = await SqlAlchemyMemoryRepository(session).commit_candidate(candidate)
            await session.commit()
        async with self.session_factory() as session:
            found = await SqlAlchemyMemoryRepository(session).find_active_by_hash(
                owner_id=user,
                scope=MemoryScopeV1.CONVERSATION,
                canonical_hash=candidate.canonical_hash,
            )
            self.assertIsNotNone(found)
            self.assertEqual(found.id, committed.id)

    async def test_quarantined_not_found_active(self) -> None:
        org, user = uuid4(), uuid4()
        await self._seed_user(user, org)
        candidate = _candidate(owner_id=user, state=MemoryStateV1.QUARANTINED)
        async with self.session_factory() as session:
            await SqlAlchemyMemoryRepository(session).commit_candidate(candidate)
            await session.commit()
        async with self.session_factory() as session:
            found = await SqlAlchemyMemoryRepository(session).find_active_by_hash(
                owner_id=user,
                scope=MemoryScopeV1.CONVERSATION,
                canonical_hash=candidate.canonical_hash,
            )
            self.assertIsNone(found)

    async def test_owner_isolation(self) -> None:
        org, user_a, user_b = uuid4(), uuid4(), uuid4()
        await self._seed_user(user_a, org)
        await self._seed_user(user_b, org)
        candidate = _candidate(owner_id=user_a)
        async with self.session_factory() as session:
            await SqlAlchemyMemoryRepository(session).commit_candidate(candidate)
            await session.commit()
        async with self.session_factory() as session:
            found = await SqlAlchemyMemoryRepository(session).find_active_by_hash(
                owner_id=user_b,
                scope=MemoryScopeV1.CONVERSATION,
                canonical_hash=candidate.canonical_hash,
            )
            self.assertIsNone(found)

    async def test_find_by_key(self) -> None:
        org, user = uuid4(), uuid4()
        await self._seed_user(user, org)
        candidate = _candidate(owner_id=user)
        async with self.session_factory() as session:
            await SqlAlchemyMemoryRepository(session).commit_candidate(candidate)
            await session.commit()
        async with self.session_factory() as session:
            found = await SqlAlchemyMemoryRepository(session).find_active_by_key(
                owner_id=user, scope=MemoryScopeV1.CONVERSATION, canonical_key="output.language"
            )
            self.assertIsNotNone(found)
            self.assertEqual(found.canonical_key, "output.language")

    async def test_append_provenance(self) -> None:
        org, user = uuid4(), uuid4()
        await self._seed_user(user, org)
        candidate = _candidate(owner_id=user)
        async with self.session_factory() as session:
            repo = SqlAlchemyMemoryRepository(session)
            committed = await repo.commit_candidate(candidate)
            merged = await repo.append_provenance(
                committed.id, (_provenance(source_id="m2", message_sequence=2),)
            )
            await session.commit()
            self.assertEqual(len(merged.provenance), 2)

    async def test_revise_supersedes(self) -> None:
        org, user = uuid4(), uuid4()
        await self._seed_user(user, org)
        candidate = _candidate(owner_id=user, trust=MemoryTrustV1.USER_EXPLICIT)
        async with self.session_factory() as session:
            repo = SqlAlchemyMemoryRepository(session)
            committed = await repo.commit_candidate(candidate)
            revised = await repo.revise(
                MemoryRevisionCommandV1(
                    record_id=committed.id,
                    corrected_content="使用 公制 单位",
                    trust=MemoryTrustV1.USER_EXPLICIT,
                    provenance=(_provenance(source_type=ProvenanceSourceTypeV1.MEMORY_COMMAND),),
                )
            )
            await session.commit()
            self.assertEqual(revised.supersedes_id, committed.id)
            self.assertEqual(revised.revision, committed.revision + 1)
            self.assertEqual(revised.state, MemoryStateV1.ACTIVE)
            self.assertEqual(revised.content, "使用 公制 单位")
            self.assertEqual(
                revised.canonical_hash,
                _hash(
                    MemoryScopeV1.CONVERSATION,
                    MemoryKindV1.PREFERENCE,
                    "output.language",
                    "使用 公制 单位",
                ),
            )
        async with self.session_factory() as session:
            repo = SqlAlchemyMemoryRepository(session)
            found = await repo.find_active_by_hash(
                owner_id=user,
                scope=MemoryScopeV1.CONVERSATION,
                canonical_hash=revised.canonical_hash,
            )
            self.assertIsNotNone(found)
            old = await repo.find_active_by_hash(
                owner_id=user,
                scope=MemoryScopeV1.CONVERSATION,
                canonical_hash=candidate.canonical_hash,
            )
            self.assertIsNone(old)

    async def test_forget_tombstone(self) -> None:
        org, user = uuid4(), uuid4()
        await self._seed_user(user, org)
        candidate = _candidate(owner_id=user)
        async with self.session_factory() as session:
            repo = SqlAlchemyMemoryRepository(session)
            committed = await repo.commit_candidate(candidate)
            receipt = await repo.forget(
                MemoryForgetCommandV1(record_id=committed.id, reason="用户要求遗忘")
            )
            await session.commit()
            self.assertEqual(receipt.record_id, committed.id)
            self.assertIsNotNone(receipt.tombstone_id)
            self.assertEqual(await self._tombstones(committed.id), 1)
        async with self.session_factory() as session:
            found = await SqlAlchemyMemoryRepository(session).find_active_by_hash(
                owner_id=user,
                scope=MemoryScopeV1.CONVERSATION,
                canonical_hash=candidate.canonical_hash,
            )
            self.assertIsNone(found)

    async def test_commit_unknown_owner_raises(self) -> None:
        async with self.session_factory() as session:
            with self.assertRaises(MemoryRecordIntegrityError):
                await SqlAlchemyMemoryRepository(session).commit_candidate(_candidate())

    async def test_find_recent_active_orders_and_limits(self) -> None:
        org, user = uuid4(), uuid4()
        await self._seed_user(user, org)
        low = _candidate(owner_id=user, canonical_key=None, importance=0.3)
        high = _candidate(owner_id=user, canonical_key=None, importance=0.9)
        mid = _candidate(owner_id=user, canonical_key=None, importance=0.6)
        async with self.session_factory() as session:
            repo = SqlAlchemyMemoryRepository(session)
            await repo.commit_candidate(low)
            await repo.commit_candidate(high)
            await repo.commit_candidate(mid)
            await session.commit()
        async with self.session_factory() as session:
            repo = SqlAlchemyMemoryRepository(session)
            top = await repo.find_recent_active(
                owner_id=user, scope=MemoryScopeV1.CONVERSATION, limit=2
            )
            self.assertEqual([record.importance for record in top], [0.9, 0.6])
            all_records = await repo.find_recent_active(
                owner_id=user, scope=MemoryScopeV1.CONVERSATION, limit=10
            )
            self.assertEqual([record.importance for record in all_records], [0.9, 0.6, 0.3])

    async def test_find_recent_active_tie_break_by_valid_from(self) -> None:
        org, user = uuid4(), uuid4()
        await self._seed_user(user, org)
        older = _candidate(
            owner_id=user,
            canonical_key=None,
            importance=0.8,
            valid_from=_NOW - timedelta(hours=1),
        )
        newer = _candidate(owner_id=user, canonical_key=None, importance=0.8, valid_from=_NOW)
        async with self.session_factory() as session:
            repo = SqlAlchemyMemoryRepository(session)
            await repo.commit_candidate(older)
            await repo.commit_candidate(newer)
            await session.commit()
        async with self.session_factory() as session:
            records = await SqlAlchemyMemoryRepository(session).find_recent_active(
                owner_id=user, scope=MemoryScopeV1.CONVERSATION, limit=10
            )
            self.assertEqual(records[0].valid_from, _NOW)
            self.assertEqual(records[1].valid_from, _NOW - timedelta(hours=1))

    async def test_find_recent_active_scope_and_owner_isolation(self) -> None:
        org, user_a, user_b = uuid4(), uuid4(), uuid4()
        await self._seed_user(user_a, org)
        await self._seed_user(user_b, org)
        conv = _candidate(owner_id=user_a, scope=MemoryScopeV1.CONVERSATION, canonical_key=None)
        task = _candidate(owner_id=user_a, scope=MemoryScopeV1.TASK, canonical_key=None)
        other = _candidate(owner_id=user_b, scope=MemoryScopeV1.CONVERSATION, canonical_key=None)
        async with self.session_factory() as session:
            repo = SqlAlchemyMemoryRepository(session)
            await repo.commit_candidate(conv)
            await repo.commit_candidate(task)
            await repo.commit_candidate(other)
            await session.commit()
        async with self.session_factory() as session:
            repo = SqlAlchemyMemoryRepository(session)
            conv_records = await repo.find_recent_active(
                owner_id=user_a, scope=MemoryScopeV1.CONVERSATION, limit=10
            )
            self.assertEqual(len(conv_records), 1)
            self.assertEqual(conv_records[0].owner_id, user_a)
            self.assertEqual(conv_records[0].scope, MemoryScopeV1.CONVERSATION)
            task_records = await repo.find_recent_active(
                owner_id=user_a, scope=MemoryScopeV1.TASK, limit=10
            )
            self.assertEqual(len(task_records), 1)
            self.assertEqual(task_records[0].scope, MemoryScopeV1.TASK)
            other_records = await repo.find_recent_active(
                owner_id=user_b, scope=MemoryScopeV1.CONVERSATION, limit=10
            )
            self.assertEqual(len(other_records), 1)
            self.assertEqual(other_records[0].owner_id, user_b)

    async def test_find_recent_active_excludes_quarantined(self) -> None:
        org, user = uuid4(), uuid4()
        await self._seed_user(user, org)
        quarantined = _candidate(
            owner_id=user, canonical_key=None, state=MemoryStateV1.QUARANTINED
        )
        async with self.session_factory() as session:
            await SqlAlchemyMemoryRepository(session).commit_candidate(quarantined)
            await session.commit()
        async with self.session_factory() as session:
            records = await SqlAlchemyMemoryRepository(session).find_recent_active(
                owner_id=user, scope=MemoryScopeV1.CONVERSATION, limit=10
            )
            self.assertEqual(records, ())

    async def test_find_active_by_conflict_group_active_siblings(self) -> None:
        org, user_a, user_b = uuid4(), uuid4(), uuid4()
        await self._seed_user(user_a, org)
        await self._seed_user(user_b, org)
        group = uuid4()
        r1 = _candidate(
            owner_id=user_a, canonical_key="output.language", conflict_group_id=group
        )
        r2 = _candidate(
            owner_id=user_a, canonical_key="output.language", conflict_group_id=group
        )
        quarantined = _candidate(
            owner_id=user_a,
            canonical_key="output.language",
            conflict_group_id=group,
            state=MemoryStateV1.QUARANTINED,
        )
        foreign = _candidate(
            owner_id=user_b, canonical_key="output.language", conflict_group_id=group
        )
        async with self.session_factory() as session:
            repo = SqlAlchemyMemoryRepository(session)
            await repo.commit_candidate(r1)
            await repo.commit_candidate(r2)
            await repo.commit_candidate(quarantined)
            await repo.commit_candidate(foreign)
            await session.commit()
        async with self.session_factory() as session:
            siblings = await SqlAlchemyMemoryRepository(
                session
            ).find_active_by_conflict_group(owner_id=user_a, conflict_group_id=group)
            self.assertEqual(len(siblings), 2)
            for sibling in siblings:
                self.assertEqual(sibling.state, MemoryStateV1.ACTIVE)
                self.assertEqual(sibling.owner_id, user_a)
                self.assertEqual(sibling.conflict_group_id, group)


if __name__ == "__main__":
    unittest.main()
