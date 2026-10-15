"""DEV-08 子批次 10：Context Pack 复用编排（reuse-or-assemble）的离线与真实 PG 验收。

离线部分用内存替身证明 `ContextPackService.obtain` 的分派：命中 `input_digest` → REUSED（只回读
复用命中、不装配、不落库），未命中 → ASSEMBLED（`assemble_context_pack` 装配 READY 新 Pack +
`save_pack` 落库，结果 `input_digest` 与 Pack 自身一致），`save_pack` 幂等重放透传为 save_outcome，
强制项超限/预留超窗失败关闭且不落库，同一输入再次 obtain 命中既有 Pack。真实 PG 部分（
`KNOWLEDGE_TEST_DATABASE_URL` 未配置时跳过）证明端到端：首次 ASSEMBLED 落库，换新 Pack id 但同
`input_digest` 的第二次 obtain 直接 REUSED 回读首个 Pack。零 LLM/零 I/O。
"""

from __future__ import annotations

import hashlib
import os
import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

from dev06_pg_support import TEST_DATABASE_URL, IsolatedDatabase, seed_foundation
from pydantic import ValidationError

from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.context_pack_repository import (
    SqlAlchemyContextPackRepository,
)
from knowledge_system.modules.memory.application.context_pack_service import (
    ContextPackObtainResultV1,
    ContextPackOutcomeV1,
    ContextPackService,
)
from knowledge_system.modules.memory.application.ports import ContextPackSaveOutcomeV1
from knowledge_system.modules.memory.public import (
    ContextAssemblyInputV1,
    ContextItemManifestV1,
    ContextItemTypeV1,
    ContextOperationProfileV1,
    ContextPackReuseHitV1,
    ContextPackStateV1,
    ContextPackV1,
    MemoryVersionSnapshotV1,
    assemble_context_pack,
)
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    ExactTokenAccountingV1,
)

_NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
_PACK_ID = uuid4()
_TASK_ID = uuid4()
_ACTOR = uuid4()
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


def _item(label: str, item_type: ContextItemTypeV1, **overrides: object) -> ContextItemManifestV1:
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


def _request(**overrides: object) -> ContextAssemblyInputV1:
    defaults: dict[str, object] = {
        "id": _PACK_ID,
        "task_id": _TASK_ID,
        "action_attempt_id": None,
        "created_by_attempt_id": None,
        "conversation_id": None,
        "actor_id": _ACTOR,
        "authorization_ref_id": None,
        "authorization_version": None,
        "policy_version": "ctx-policy-v1",
        "memory_versions": MemoryVersionSnapshotV1(),
        "runtime_version": "rt-v1",
        "plan_version": None,
        "agent_manifest_version": "agent-v1",
        "model_revision": "model-v1",
        "prompt_version": "prompt-v1",
        "tool_schema_versions": (),
        "output_schema_id": None,
        "output_schema_version": None,
        "token_accounting": _exact_accounting(),
        "body_artifact_id": _BODY,
        "created_at": _NOW,
        "max_context_tokens": 32768,
        "reserved_output_tokens": 2048,
        "protocol_overhead_tokens": 512,
        "safety_margin_tokens": 128,
        "operation_profile": ContextOperationProfileV1.KNOWLEDGE_RESEARCH,
        "mandatory_items": (
            _item("policy", ContextItemTypeV1.SYSTEM_POLICY, token_count=1000),
        ),
        "elastic_candidates": (
            _item("mem1", ContextItemTypeV1.MEMORY, token_count=100),
        ),
    }
    defaults.update(overrides)
    return ContextAssemblyInputV1(**defaults)


def _hit_from_pack(pack: ContextPackV1) -> ContextPackReuseHitV1:
    return ContextPackReuseHitV1(
        id=pack.id,
        task_id=pack.task_id,
        state=pack.state,
        pack_hash=pack.pack_hash,
        input_digest=pack.input_digest,
        input_tokens=pack.input_tokens,
        body_artifact_id=pack.body_artifact_id,
        created_at=pack.created_at,
        consumed_at=None,
    )


class _FakeContextPackRepository:
    """内存替身：按构造返回固定命中或未命中，并记录 save/query 调用。"""

    def __init__(
        self,
        *,
        hit: ContextPackReuseHitV1 | None = None,
        save_outcome: ContextPackSaveOutcomeV1 = ContextPackSaveOutcomeV1.INSERTED,
    ) -> None:
        self._hit = hit
        self._save_outcome = save_outcome
        self.saved: list[ContextPackV1] = []
        self.queries: list[tuple[UUID, str]] = []

    async def find_reusable_by_input_digest(
        self, *, task_id: UUID, input_digest: str
    ) -> ContextPackReuseHitV1 | None:
        self.queries.append((task_id, input_digest))
        return self._hit

    async def save_pack(self, *, pack: ContextPackV1) -> ContextPackSaveOutcomeV1:
        self.saved.append(pack)
        return self._save_outcome


class _StatefulFakeContextPackRepository:
    """内存替身：按 (task_id, input_digest) 存命中，模拟权威 Store 的内容寻址去重。"""

    def __init__(self) -> None:
        self._by_digest: dict[tuple[UUID, str], ContextPackReuseHitV1] = {}
        self.saved: list[ContextPackV1] = []

    async def find_reusable_by_input_digest(
        self, *, task_id: UUID, input_digest: str
    ) -> ContextPackReuseHitV1 | None:
        return self._by_digest.get((task_id, input_digest))

    async def save_pack(self, *, pack: ContextPackV1) -> ContextPackSaveOutcomeV1:
        self.saved.append(pack)
        self._by_digest[(pack.task_id, pack.input_digest)] = _hit_from_pack(pack)
        return ContextPackSaveOutcomeV1.INSERTED


class ContextPackServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_obtain_reuses_when_digest_hits(self) -> None:
        pack = assemble_context_pack(_request())
        fake = _FakeContextPackRepository(hit=_hit_from_pack(pack))
        result = await ContextPackService(fake).obtain(_request())
        self.assertEqual(result.outcome, ContextPackOutcomeV1.REUSED)
        self.assertIsNotNone(result.reused)
        self.assertIsNone(result.pack)
        self.assertIsNone(result.save_outcome)
        self.assertEqual(fake.saved, [])

    async def test_obtain_assembles_when_digest_misses(self) -> None:
        fake = _FakeContextPackRepository(hit=None)
        result = await ContextPackService(fake).obtain(_request())
        self.assertEqual(result.outcome, ContextPackOutcomeV1.ASSEMBLED)
        self.assertIsNone(result.reused)
        assert result.pack is not None
        self.assertEqual(result.pack.state, ContextPackStateV1.READY)
        self.assertEqual(result.pack.id, _PACK_ID)
        self.assertEqual(result.save_outcome, ContextPackSaveOutcomeV1.INSERTED)
        self.assertEqual(len(fake.saved), 1)

    async def test_obtain_result_digest_matches_pack(self) -> None:
        result = await ContextPackService(_FakeContextPackRepository(hit=None)).obtain(_request())
        assert result.pack is not None
        self.assertEqual(result.input_digest, result.pack.input_digest)

    async def test_obtain_propagates_idempotent_replay(self) -> None:
        fake = _FakeContextPackRepository(
            hit=None, save_outcome=ContextPackSaveOutcomeV1.IDEMPOTENT_REPLAY
        )
        result = await ContextPackService(fake).obtain(_request())
        self.assertEqual(result.outcome, ContextPackOutcomeV1.ASSEMBLED)
        self.assertEqual(result.save_outcome, ContextPackSaveOutcomeV1.IDEMPOTENT_REPLAY)

    async def test_obtain_fails_closed_without_saving(self) -> None:
        request = _request(
            max_context_tokens=1000,
            reserved_output_tokens=100,
            protocol_overhead_tokens=0,
            safety_margin_tokens=0,
            mandatory_items=(
                _item("policy", ContextItemTypeV1.SYSTEM_POLICY, token_count=1000),
            ),
        )
        fake = _FakeContextPackRepository(hit=None)
        with self.assertRaises(ValueError):
            await ContextPackService(fake).obtain(request)
        self.assertEqual(fake.saved, [])

    async def test_obtain_second_request_reuses_first_pack(self) -> None:
        fake = _StatefulFakeContextPackRepository()
        service = ContextPackService(fake)
        first = await service.obtain(_request(id=uuid4()))
        self.assertEqual(first.outcome, ContextPackOutcomeV1.ASSEMBLED)
        second = await service.obtain(_request(id=uuid4()))
        self.assertEqual(second.outcome, ContextPackOutcomeV1.REUSED)
        assert second.reused is not None and first.pack is not None
        self.assertEqual(second.reused.id, first.pack.id)
        self.assertEqual(len(fake.saved), 1)

    def test_result_is_strict_contract(self) -> None:
        with self.assertRaises(ValidationError):
            ContextPackObtainResultV1(
                outcome=ContextPackOutcomeV1.REUSED, input_digest="x" * 64, bogus="y"
            )


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class ContextPackServicePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev08_context_pack_service_{os.getpid()}_{uuid4().hex[:8]}"
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

    def _request_for(
        self, ids: dict[str, UUID], artifacts: dict[str, UUID]
    ) -> ContextAssemblyInputV1:
        return _request(
            task_id=ids["task"],
            actor_id=ids["user"],
            conversation_id=ids["conversation"],
            body_artifact_id=artifacts["query"],
        )

    async def test_obtain_assembles_then_reuses_against_pg(self) -> None:
        ids, artifacts = await self._seed()
        first_request = self._request_for(ids, artifacts).model_copy(update={"id": uuid4()})
        second_request = self._request_for(ids, artifacts).model_copy(update={"id": uuid4()})

        async with self.session_factory() as session:
            service = ContextPackService(SqlAlchemyContextPackRepository(session))
            first = await service.obtain(first_request)
            await session.commit()

        async with self.session_factory() as session:
            service = ContextPackService(SqlAlchemyContextPackRepository(session))
            second = await service.obtain(second_request)

        self.assertEqual(first.outcome, ContextPackOutcomeV1.ASSEMBLED)
        self.assertEqual(first.save_outcome, ContextPackSaveOutcomeV1.INSERTED)
        self.assertEqual(second.outcome, ContextPackOutcomeV1.REUSED)
        assert first.pack is not None and second.reused is not None
        self.assertEqual(second.reused.id, first.pack.id)
        self.assertEqual(second.input_digest, first.input_digest)


if __name__ == "__main__":
    unittest.main()
