"""DEV-08 子批次 13：Pin 召回通道（`pinned` 标志 + `pin` 命令 + `find_pinned` 通道）验收。

离线：证明 `record_from_candidate`/`memory_from_record` 互转保留 `pinned`（新记录恒 false）、
`MemoryPinCommandV1` 契约。真实 PG（`KNOWLEDGE_TEST_DATABASE_URL` 未配置时跳过）：以真实
`SqlAlchemyMemoryRepository` 验证 Pin/Unpin 切换持久化、`find_pinned` 只召回被 Pin 的 ACTIVE
记录且确定性排序、纠正（revise）继承 Pin 标志、未知记录 Pin 失败关闭。零 LLM/零 embedding/零 I/O。
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from dev06_pg_support import TEST_DATABASE_URL, IsolatedDatabase
from sqlalchemy import text

from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.memory_repository import (
    MemoryRecordIntegrityError,
    SqlAlchemyMemoryRepository,
    memory_from_record,
    record_from_candidate,
)
from knowledge_system.modules.memory.application.ports import (
    GovernedMemoryCandidateV1,
    MemoryPinCommandV1,
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


def _hash(scope: MemoryScopeV1, kind: MemoryKindV1, key: str | None, statement: str) -> str:
    return compute_canonical_hash(scope, kind, key, normalize_memory_statement(statement))


def _provenance(**overrides: object) -> ProvenanceRefV1:
    defaults: dict[str, object] = {
        "source_type": ProvenanceSourceTypeV1.MESSAGE,
        "source_id": "m1",
        "content_hash": "a" * 64,
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
        "canonical_key": None,
        "content": "使用 SI 单位",
        "canonical_hash": _hash(
            MemoryScopeV1.CONVERSATION, MemoryKindV1.PREFERENCE, None, "使用 SI 单位"
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


class MemoryPinMappingTests(unittest.TestCase):
    def test_new_record_defaults_unpinned(self) -> None:
        orm = record_from_candidate(_candidate(), organization_id=_ORG, record_id=uuid4())
        domain = memory_from_record(orm)
        self.assertFalse(domain.pinned)

    def test_pin_command_contract(self) -> None:
        record_id = uuid4()
        command = MemoryPinCommandV1(record_id=record_id, pinned=True)
        self.assertEqual(command.record_id, record_id)
        self.assertTrue(command.pinned)


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class SqlAlchemyMemoryPinPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev08_memory_pin_{os.getpid()}_{uuid4().hex[:8]}"
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

    async def _commit(self, user: object, **overrides: object) -> object:
        async with self.session_factory() as session:
            record = await SqlAlchemyMemoryRepository(session).commit_candidate(
                _candidate(owner_id=user, **overrides)
            )
            await session.commit()
            return record.id

    async def test_pin_toggles_and_persists(self) -> None:
        org, user = uuid4(), uuid4()
        await self._seed_user(user, org)
        record_id = await self._commit(user)

        async with self.session_factory() as session:
            repo = SqlAlchemyMemoryRepository(session)
            pinned = await repo.pin(MemoryPinCommandV1(record_id=record_id, pinned=True))
            await session.commit()
            self.assertTrue(pinned.pinned)

        async with self.session_factory() as session:
            found = await SqlAlchemyMemoryRepository(session).find_pinned(
                owner_id=user, scope=MemoryScopeV1.CONVERSATION, limit=10
            )
            self.assertEqual([record.id for record in found], [record_id])

        async with self.session_factory() as session:
            repo = SqlAlchemyMemoryRepository(session)
            unpinned = await repo.pin(MemoryPinCommandV1(record_id=record_id, pinned=False))
            await session.commit()
            self.assertFalse(unpinned.pinned)

        async with self.session_factory() as session:
            found = await SqlAlchemyMemoryRepository(session).find_pinned(
                owner_id=user, scope=MemoryScopeV1.CONVERSATION, limit=10
            )
            self.assertEqual(found, ())

    async def test_find_pinned_excludes_unpinned_and_quarantined(self) -> None:
        org, user = uuid4(), uuid4()
        await self._seed_user(user, org)
        pinned_id = await self._commit(user)
        unpinned_id = await self._commit(user)
        quarantined_id = await self._commit(user, state=MemoryStateV1.QUARANTINED)
        async with self.session_factory() as session:
            repo = SqlAlchemyMemoryRepository(session)
            await repo.pin(MemoryPinCommandV1(record_id=pinned_id, pinned=True))
            await repo.pin(MemoryPinCommandV1(record_id=quarantined_id, pinned=True))
            await session.commit()

        async with self.session_factory() as session:
            found = await SqlAlchemyMemoryRepository(session).find_pinned(
                owner_id=user, scope=MemoryScopeV1.CONVERSATION, limit=10
            )
        self.assertEqual([record.id for record in found], [pinned_id])
        self.assertNotIn(unpinned_id, [record.id for record in found])
        self.assertNotIn(quarantined_id, [record.id for record in found])

    async def test_find_pinned_orders_by_importance(self) -> None:
        org, user = uuid4(), uuid4()
        await self._seed_user(user, org)
        low_id = await self._commit(user, importance=0.3)
        high_id = await self._commit(user, importance=0.9)
        mid_id = await self._commit(user, importance=0.6)
        async with self.session_factory() as session:
            repo = SqlAlchemyMemoryRepository(session)
            for record_id in (low_id, high_id, mid_id):
                await repo.pin(MemoryPinCommandV1(record_id=record_id, pinned=True))
            await session.commit()

        async with self.session_factory() as session:
            found = await SqlAlchemyMemoryRepository(session).find_pinned(
                owner_id=user, scope=MemoryScopeV1.CONVERSATION, limit=10
            )
        self.assertEqual([record.id for record in found], [high_id, mid_id, low_id])

    async def test_pin_unknown_record_raises(self) -> None:
        org, user = uuid4(), uuid4()
        await self._seed_user(user, org)
        async with self.session_factory() as session:
            with self.assertRaises(MemoryRecordIntegrityError):
                await SqlAlchemyMemoryRepository(session).pin(
                    MemoryPinCommandV1(record_id=uuid4(), pinned=True)
                )

    async def test_revise_carries_pin_over(self) -> None:
        org, user = uuid4(), uuid4()
        await self._seed_user(user, org)
        record_id = await self._commit(user)
        async with self.session_factory() as session:
            repo = SqlAlchemyMemoryRepository(session)
            await repo.pin(MemoryPinCommandV1(record_id=record_id, pinned=True))
            revised = await repo.revise(
                MemoryRevisionCommandV1(
                    record_id=record_id,
                    corrected_content="使用 公制 单位",
                    trust=MemoryTrustV1.USER_EXPLICIT,
                    provenance=(_provenance(source_type=ProvenanceSourceTypeV1.MEMORY_COMMAND),),
                )
            )
            await session.commit()
            self.assertTrue(revised.pinned)
            self.assertEqual(revised.supersedes_id, record_id)

        async with self.session_factory() as session:
            found = await SqlAlchemyMemoryRepository(session).find_pinned(
                owner_id=user, scope=MemoryScopeV1.CONVERSATION, limit=10
            )
            self.assertEqual([record.id for record in found], [revised.id])


if __name__ == "__main__":
    unittest.main()
