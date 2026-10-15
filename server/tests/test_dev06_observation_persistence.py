"""DEV-06 不可变观察投影持久层验收：
`workflow.task_observation` / `workflow.observation_disposition`。

离线部分证明 `record_from_observation`/`observation_from_record` 把判别联合 payload 确定性压平与
重建，以及 Disposition 的互转；真实 PG 部分（`KNOWLEDGE_TEST_DATABASE_URL` 未配置时跳过）验证
迁移 0011 建表、Observation 按 `observation_id` 幂等插入、Disposition 按 `disposition_event_id`
幂等追加，以及完整往返。
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4

from dev06_pg_support import (
    DIGEST,
    TEST_DATABASE_URL,
    IsolatedDatabase,
    seed_foundation,
)

from knowledge_system.infrastructure.persistence import (
    Base,
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.observation_repository import (
    TaskObservationRepository,
    disposition_event_from_record,
    disposition_record_from_event,
    observation_from_record,
    record_from_observation,
)
from knowledge_system.modules.tasking.domain.core_types import VersionedRefV1
from knowledge_system.modules.tasking.domain.observation import (
    PROJECTION_PROFILE_VERSION,
    ObservationDispositionEventV1,
    ObservationDispositionV1,
    ObservationSourceKindV1,
    ResultAdoptedPayloadV1,
    TaskObservationV1,
    observation_class_for,
    observation_id,
)

_EVENT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def _ref() -> VersionedRefV1:
    return VersionedRefV1(ref_id=uuid4(), version=1, integrity_digest=DIGEST)


def _created_at() -> datetime:
    return datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


def make_observation(task_id: UUID, *, source_ordinal: int = 0) -> TaskObservationV1:
    source_kind = ObservationSourceKindV1.RESULT_ADOPTED
    return TaskObservationV1(
        contract_version="task_observation_v1",
        observation_id=observation_id(
            task_id, _EVENT_ID, source_ordinal, PROJECTION_PROFILE_VERSION
        ),
        task_id=task_id,
        source_event_id=_EVENT_ID,
        source_event_sequence=1,
        source_ordinal=source_ordinal,
        source_kind=source_kind,
        observation_class=observation_class_for(source_kind),
        source_plan_version=1,
        created_at=_created_at(),
        payload=ResultAdoptedPayloadV1(source_kind="RESULT_ADOPTED", adopted_refs=(_ref(),)),
        integrity_digest=DIGEST,
    )


def make_disposition(
    observation: TaskObservationV1,
    disposition: ObservationDispositionV1 = ObservationDispositionV1.RESOLVED,
) -> ObservationDispositionEventV1:
    return ObservationDispositionEventV1(
        contract_version="observation_disposition_event_v1",
        disposition_event_id=uuid4(),
        observation_id=observation.observation_id,
        task_id=observation.task_id,
        disposition=disposition,
        reason_code="SUPERSEDED_BY_NEWER_FACT",
        caused_by_event_id=observation.source_event_id,
        created_at=_created_at(),
    )


class ObservationMetadataTests(unittest.TestCase):
    def test_tables_registered(self) -> None:
        self.assertIn("workflow.task_observation", Base.metadata.tables)
        self.assertIn("workflow.observation_disposition", Base.metadata.tables)

    def test_observation_pk_and_fk(self) -> None:
        table = Base.metadata.tables["workflow.task_observation"]
        self.assertEqual([c.name for c in table.primary_key.columns], ["observation_id"])
        fks = list(table.c["task_id"].foreign_keys)
        self.assertEqual(len(fks), 1)
        self.assertEqual(fks[0].column.table.name, "intelligent_task")

    def test_disposition_pk_and_fk(self) -> None:
        table = Base.metadata.tables["workflow.observation_disposition"]
        self.assertEqual(
            [c.name for c in table.primary_key.columns], ["disposition_event_id"]
        )
        fks = list(table.c["observation_id"].foreign_keys)
        self.assertEqual(fks[0].column.table.name, "task_observation")


class ObservationMappingTests(unittest.TestCase):
    def test_record_flattens_fields(self) -> None:
        observation = make_observation(uuid4())
        record = record_from_observation(observation)
        self.assertEqual(record.contract_version, "task_observation_v1")
        self.assertEqual(record.source_kind, "RESULT_ADOPTED")
        self.assertEqual(record.observation_class, "ADOPTED_RESULT")
        self.assertEqual(record.integrity_digest, DIGEST)
        self.assertIn("adopted_refs", record.payload_json)

    def test_observation_roundtrip(self) -> None:
        observation = make_observation(uuid4())
        self.assertEqual(observation_from_record(record_from_observation(observation)), observation)

    def test_disposition_roundtrip(self) -> None:
        observation = make_observation(uuid4())
        event = make_disposition(observation)
        self.assertEqual(disposition_event_from_record(disposition_record_from_event(event)), event)

    def test_disposition_flattens_enum(self) -> None:
        observation = make_observation(uuid4())
        record = disposition_record_from_event(make_disposition(observation))
        self.assertEqual(record.disposition, "RESOLVED")


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class ObservationPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev06_observation_{os.getpid()}_{uuid4().hex[:8]}"
        self.isolated = IsolatedDatabase(TEST_DATABASE_URL, self.dbname)
        self.url = await self.isolated.__aenter__()
        self.engine = build_async_engine(DatabaseEngineSettings(url=self.url))
        self.session_factory = build_session_factory(self.engine)
        self.ids = {
            name: uuid4()
            for name in ("org", "user", "conversation", "task", "message", "input")
        }
        await seed_foundation(self.engine, self.ids, {"query": uuid4()})

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        await self.isolated.__aexit__(None, None, None)

    async def test_insert_idempotent_and_roundtrip(self) -> None:
        observation = make_observation(self.ids["task"])
        async with self.session_factory() as session:
            repo = TaskObservationRepository(session)
            first = await repo.insert(observation)
            second = await repo.insert(observation)
            await session.commit()
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.observation_id, observation.observation_id)

        async with self.session_factory() as session:
            loaded = await TaskObservationRepository(session).get(observation.observation_id)
        self.assertEqual(loaded, observation)

    async def test_disposition_append_idempotent_and_list(self) -> None:
        observation = make_observation(self.ids["task"])
        event = make_disposition(observation)
        async with self.session_factory() as session:
            repo = TaskObservationRepository(session)
            await repo.insert(observation)
            first = await repo.append_disposition(event)
            second = await repo.append_disposition(event)
            await session.commit()
        self.assertTrue(first.created)
        self.assertFalse(second.created)

        async with self.session_factory() as session:
            dispositions = await TaskObservationRepository(session).list_dispositions(
                observation.observation_id
            )
        self.assertEqual(dispositions, (event,))

    async def test_get_returns_none_for_missing(self) -> None:
        async with self.session_factory() as session:
            self.assertIsNone(await TaskObservationRepository(session).get(uuid4()))


if __name__ == "__main__":
    unittest.main()
