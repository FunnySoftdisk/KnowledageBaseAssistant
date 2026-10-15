"""DEV-08 子批次 8：不可变 Context Pack 权威 Store 的离线映射与真实 PG 验收。

离线部分证明 `record_from_pack`/`item_record_from_manifest` 头行与 Item 的确定性列映射：
`context_policy_version` 等预算/版本列改名、`tool_schema_versions` 折叠为 dict、`EXACT` 落
`tokenizer_revision`/`chat_template_hash` 而 `ESTIMATED_DEV` 落 NULL、强制项无来源/桶落 NULL、
弹性项 `source_id` 严格 UUID 解析失败关闭；真实 PG 部分（`KNOWLEDGE_TEST_DATABASE_URL` 未配置时
跳过）验证迁移 0013 放宽可空后 `save_pack` 首次落库、按 `id` 幂等重放、Item 按 position 有序
持久化且强制项空列确实落 NULL。
"""

from __future__ import annotations

import hashlib
import os
import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

from dev06_pg_support import TEST_DATABASE_URL, IsolatedDatabase, seed_foundation
from sqlalchemy import select

from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.attempt_models import (
    ContextPackItemRecord,
    ContextPackRecord,
)
from knowledge_system.infrastructure.persistence.context_pack_repository import (
    ContextPackIntegrityError,
    SqlAlchemyContextPackRepository,
    item_record_from_manifest,
    pack_reuse_hit_from_record,
    record_from_pack,
)
from knowledge_system.modules.memory.application.ports import ContextPackSaveOutcomeV1
from knowledge_system.modules.memory.domain.context_assembly import (
    ContextPackStateV1,
    ContextPackV1,
    ToolSchemaVersionV1,
)
from knowledge_system.modules.memory.domain.context_pack import (
    ContextItemManifestV1,
    ContextItemTypeV1,
)
from knowledge_system.modules.memory.domain.memory_types import (
    MemoryVersionSnapshotV1,
    ProvenanceRefV1,
    ProvenanceSourceTypeV1,
)
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    EstimatedDevTokenAccountingV1,
    ExactTokenAccountingV1,
)

_NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
_PACK_ID = uuid4()
_TASK_ID = uuid4()
_ACTOR = uuid4()
_CONVERSATION = uuid4()
_BODY = uuid4()
_NAMESPACE = UUID(int=0)


def _uuid(label: str) -> UUID:
    return uuid5(_NAMESPACE, label)


def _exact_accounting() -> ExactTokenAccountingV1:
    return ExactTokenAccountingV1(
        mode="EXACT",
        estimated=False,
        exact_input_tokens=1000,
        tokenizer_revision="tok-v1",
        chat_template_hash="a" * 64,
    )


def _estimated_accounting() -> EstimatedDevTokenAccountingV1:
    return EstimatedDevTokenAccountingV1(
        mode="ESTIMATED_DEV",
        estimated=True,
        estimated_input_tokens=1000,
        estimator_revision="est-v1",
        serialized_request_bytes=4096,
        protocol_overhead_tokens=512,
    )


def _source_ref(**overrides: object) -> ProvenanceRefV1:
    defaults: dict[str, object] = {
        "source_type": ProvenanceSourceTypeV1.TASK_ARTIFACT,
        "source_id": str(uuid4()),
        "source_version": "v1",
        "content_hash": "a" * 64,
        "message_sequence": None,
    }
    defaults.update(overrides)
    return ProvenanceRefV1(**defaults)


def _item(
    label: str, item_type: ContextItemTypeV1, **overrides: object
) -> ContextItemManifestV1:
    defaults: dict[str, object] = {
        "content_hash": hashlib.sha256(label.encode("utf-8")).hexdigest(),
        "token_count": 10,
        "priority": 0,
        "rank_score": None,
        "compression_level": 0,
        "selection_reason": "test",
        "trust": "SYSTEM",
    }
    defaults.update(overrides)
    return ContextItemManifestV1(item_id=_uuid(label), item_type=item_type, **defaults)


def _pack(**overrides: object) -> ContextPackV1:
    defaults: dict[str, object] = {
        "id": _PACK_ID,
        "task_id": _TASK_ID,
        "action_attempt_id": None,
        "created_by_attempt_id": None,
        "conversation_id": _CONVERSATION,
        "actor_id": _ACTOR,
        "authorization_ref_id": None,
        "authorization_version": None,
        "policy_version": "ctx-policy-v1",
        "memory_versions": MemoryVersionSnapshotV1(
            conversation_version=1, user_private_version=2, task_version=3
        ),
        "runtime_version": "rt-v1",
        "plan_version": 1,
        "agent_manifest_version": "agent-v1",
        "model_revision": "model-v1",
        "prompt_version": "prompt-v1",
        "tool_schema_versions": (ToolSchemaVersionV1(name="tools.search", version="v1"),),
        "output_schema_id": "out-v1",
        "output_schema_version": "v1",
        "token_accounting": _exact_accounting(),
        "max_context_tokens": 32768,
        "reserved_output_tokens": 2048,
        "protocol_overhead_tokens": 512,
        "safety_margin_tokens": 128,
        "input_tokens": 1100,
        "items": (
            _item("policy", ContextItemTypeV1.SYSTEM_POLICY),
            _item("mem1", ContextItemTypeV1.MEMORY, source_ref=_source_ref()),
        ),
        "body_artifact_id": _BODY,
        "pack_hash": "b" * 64,
        "input_digest": "c" * 64,
        "state": ContextPackStateV1.READY,
        "created_at": _NOW,
    }
    defaults.update(overrides)
    return ContextPackV1(**defaults)


class ContextPackMappingTests(unittest.TestCase):
    def test_record_from_pack_maps_header(self) -> None:
        orm = record_from_pack(_pack())
        self.assertEqual(orm.id, _PACK_ID)
        self.assertEqual(orm.task_id, _TASK_ID)
        self.assertEqual(orm.actor_id, _ACTOR)
        self.assertEqual(orm.conversation_id, _CONVERSATION)
        self.assertIsNone(orm.action_attempt_id)
        self.assertIsNone(orm.created_by_attempt_id)
        self.assertEqual(orm.state, "READY")
        self.assertEqual(orm.context_policy_version, "ctx-policy-v1")
        self.assertEqual(orm.conversation_memory_version, 1)
        self.assertEqual(orm.user_private_memory_version, 2)
        self.assertEqual(orm.task_memory_version, 3)
        self.assertEqual(orm.max_context, 32768)
        self.assertEqual(orm.reserved_output, 2048)
        self.assertEqual(orm.protocol_overhead, 512)
        self.assertEqual(orm.safety_margin, 128)
        self.assertEqual(orm.input_tokens, 1100)
        self.assertEqual(orm.tool_schema_versions, {"tools.search": "v1"})
        self.assertEqual(orm.output_schema_id, "out-v1")
        self.assertEqual(orm.output_schema_version, "v1")
        self.assertEqual(orm.tokenizer_revision, "tok-v1")
        self.assertEqual(orm.chat_template_hash, "a" * 64)
        self.assertEqual(orm.body_artifact_id, _BODY)
        self.assertEqual(orm.pack_hash, "b" * 64)
        self.assertEqual(orm.input_digest, "c" * 64)
        self.assertEqual(orm.created_at, _NOW)
        self.assertIsNone(orm.consumed_at)

    def test_record_from_pack_estimated_null_tokenizer(self) -> None:
        orm = record_from_pack(_pack(token_accounting=_estimated_accounting()))
        self.assertIsNone(orm.tokenizer_revision)
        self.assertIsNone(orm.chat_template_hash)

    def test_mandatory_item_null_source_and_bucket(self) -> None:
        orm = item_record_from_manifest(
            _PACK_ID, 1, _item("policy", ContextItemTypeV1.SYSTEM_POLICY)
        )
        self.assertEqual(orm.item_id, _uuid("policy"))
        self.assertEqual(orm.item_type, "SYSTEM_POLICY")
        self.assertIsNone(orm.source_type)
        self.assertIsNone(orm.source_id)
        self.assertIsNone(orm.source_version)
        self.assertIsNone(orm.bucket)
        self.assertIsNone(orm.rank_score)

    def test_elastic_item_maps_source_and_bucket(self) -> None:
        ref = _source_ref()
        orm = item_record_from_manifest(
            _PACK_ID, 2, _item("mem1", ContextItemTypeV1.MEMORY, source_ref=ref)
        )
        self.assertEqual(orm.source_type, ref.source_type.value)
        self.assertEqual(orm.source_id, UUID(ref.source_id))
        self.assertEqual(orm.source_version, "v1")
        self.assertEqual(orm.bucket, "MEMORY")

    def test_observation_maps_task_artifact_bucket(self) -> None:
        orm = item_record_from_manifest(
            _PACK_ID,
            1,
            _item("obs1", ContextItemTypeV1.OBSERVATION, source_ref=_source_ref()),
        )
        self.assertEqual(orm.bucket, "TASK_ARTIFACT")

    def test_non_uuid_source_id_fails_closed(self) -> None:
        ref = _source_ref(source_id="not-a-uuid")
        item = _item("mem1", ContextItemTypeV1.MEMORY, source_ref=ref)
        with self.assertRaises(ContextPackIntegrityError):
            item_record_from_manifest(_PACK_ID, 1, item)

    def test_pack_reuse_hit_maps_header_facts(self) -> None:
        hit = pack_reuse_hit_from_record(record_from_pack(_pack()))
        self.assertEqual(hit.id, _PACK_ID)
        self.assertEqual(hit.task_id, _TASK_ID)
        self.assertEqual(hit.state, ContextPackStateV1.READY)
        self.assertEqual(hit.pack_hash, "b" * 64)
        self.assertEqual(hit.input_digest, "c" * 64)
        self.assertEqual(hit.input_tokens, 1100)
        self.assertEqual(hit.body_artifact_id, _BODY)
        self.assertEqual(hit.created_at, _NOW)
        self.assertIsNone(hit.consumed_at)

    def test_pack_reuse_hit_maps_consumed_at(self) -> None:
        header = record_from_pack(_pack())
        header.consumed_at = _NOW
        hit = pack_reuse_hit_from_record(header)
        self.assertEqual(hit.consumed_at, _NOW)


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class SqlAlchemyContextPackRepositoryPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev08_context_pack_{os.getpid()}_{uuid4().hex[:8]}"
        self.isolated = IsolatedDatabase(TEST_DATABASE_URL, self.dbname)
        self.url = await self.isolated.__aenter__()
        self.engine = build_async_engine(DatabaseEngineSettings(url=self.url))
        self.session_factory = build_session_factory(self.engine)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        await self.isolated.__aexit__(None, None, None)

    async def _seed(self) -> tuple[dict[str, UUID], dict[str, UUID]]:
        ids: dict[str, UUID] = {
            "org": uuid4(),
            "user": uuid4(),
            "conversation": uuid4(),
            "task": uuid4(),
            "message": uuid4(),
            "input": uuid4(),
        }
        artifacts: dict[str, UUID] = {"query": uuid4()}
        await seed_foundation(self.engine, ids, artifacts)
        return ids, artifacts

    def _pack_for(self, ids: dict[str, UUID], artifacts: dict[str, UUID]) -> ContextPackV1:
        return _pack(
            task_id=ids["task"],
            actor_id=ids["user"],
            conversation_id=ids["conversation"],
            body_artifact_id=artifacts["query"],
        )

    async def test_save_pack_inserts_and_replay_is_idempotent(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts)
        async with self.session_factory() as session:
            repo = SqlAlchemyContextPackRepository(session)
            self.assertEqual(await repo.save_pack(pack=pack), ContextPackSaveOutcomeV1.INSERTED)
            await session.commit()
        async with self.session_factory() as session:
            repo = SqlAlchemyContextPackRepository(session)
            self.assertEqual(
                await repo.save_pack(pack=pack), ContextPackSaveOutcomeV1.IDEMPOTENT_REPLAY
            )
            await session.commit()
        async with self.session_factory() as session:
            header = await session.get(ContextPackRecord, pack.id)
            self.assertIsNotNone(header)
            assert header is not None
            self.assertEqual(header.pack_hash, "b" * 64)
            self.assertEqual(header.input_digest, "c" * 64)
            items = list(
                await session.scalars(
                    select(ContextPackItemRecord)
                    .where(ContextPackItemRecord.context_pack_id == pack.id)
                    .order_by(ContextPackItemRecord.position)
                )
            )
            self.assertEqual([item.position for item in items], [1, 2])
            self.assertEqual(
                [item.item_id for item in items], [_uuid("policy"), _uuid("mem1")]
            )

    async def test_mandatory_item_persists_null_source_columns(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts)
        pack = pack.model_copy(
            update={"items": (_item("policy", ContextItemTypeV1.SYSTEM_POLICY),)}
        )
        async with self.session_factory() as session:
            await SqlAlchemyContextPackRepository(session).save_pack(pack=pack)
            await session.commit()
        async with self.session_factory() as session:
            item = await session.get(ContextPackItemRecord, (pack.id, 1))
            self.assertIsNotNone(item)
            assert item is not None
            self.assertIsNone(item.source_type)
            self.assertIsNone(item.source_id)
            self.assertIsNone(item.source_version)
            self.assertIsNone(item.bucket)
            self.assertIsNone(item.rank_score)

    async def test_find_reusable_by_input_digest_returns_ready_hit(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts)
        async with self.session_factory() as session:
            await SqlAlchemyContextPackRepository(session).save_pack(pack=pack)
            await session.commit()
        async with self.session_factory() as session:
            hit = await SqlAlchemyContextPackRepository(session).find_reusable_by_input_digest(
                task_id=pack.task_id, input_digest=pack.input_digest
            )
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertEqual(hit.id, pack.id)
            self.assertEqual(hit.state, ContextPackStateV1.READY)
            self.assertEqual(hit.pack_hash, pack.pack_hash)
            self.assertEqual(hit.input_digest, pack.input_digest)

    async def test_find_reusable_by_input_digest_miss_different_digest(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts)
        async with self.session_factory() as session:
            await SqlAlchemyContextPackRepository(session).save_pack(pack=pack)
            await session.commit()
        async with self.session_factory() as session:
            hit = await SqlAlchemyContextPackRepository(session).find_reusable_by_input_digest(
                task_id=pack.task_id, input_digest="d" * 64
            )
            self.assertIsNone(hit)

    async def test_find_reusable_by_input_digest_miss_different_task(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts)
        async with self.session_factory() as session:
            await SqlAlchemyContextPackRepository(session).save_pack(pack=pack)
            await session.commit()
        async with self.session_factory() as session:
            hit = await SqlAlchemyContextPackRepository(session).find_reusable_by_input_digest(
                task_id=uuid4(), input_digest=pack.input_digest
            )
            self.assertIsNone(hit)

    async def test_find_reusable_returns_consumed_hit(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts)
        async with self.session_factory() as session:
            await SqlAlchemyContextPackRepository(session).save_pack(pack=pack)
            await session.commit()
        async with self.session_factory() as session:
            header = await session.get(ContextPackRecord, pack.id)
            assert header is not None
            header.state = "CONSUMED"
            await session.commit()
        async with self.session_factory() as session:
            hit = await SqlAlchemyContextPackRepository(session).find_reusable_by_input_digest(
                task_id=pack.task_id, input_digest=pack.input_digest
            )
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertEqual(hit.state, ContextPackStateV1.CONSUMED)

    async def test_find_reusable_excludes_failed(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts)
        async with self.session_factory() as session:
            await SqlAlchemyContextPackRepository(session).save_pack(pack=pack)
            await session.commit()
        async with self.session_factory() as session:
            header = await session.get(ContextPackRecord, pack.id)
            assert header is not None
            header.state = "FAILED"
            await session.commit()
        async with self.session_factory() as session:
            hit = await SqlAlchemyContextPackRepository(session).find_reusable_by_input_digest(
                task_id=pack.task_id, input_digest=pack.input_digest
            )
            self.assertIsNone(hit)


if __name__ == "__main__":
    unittest.main()
