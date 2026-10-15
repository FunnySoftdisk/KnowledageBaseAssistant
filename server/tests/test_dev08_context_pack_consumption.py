"""DEV-08 子批次 12：模型网关 Context Pack 消费门（`consume_context_pack`）的离线与真实 PG 验收。

离线（假 port）：按存储头真值表验证确定性失败关闭——缺失 `CONTEXT_PACK_NOT_FOUND`、hash 不匹配
`CONTEXT_PACK_HASH_MISMATCH`、input_digest 不匹配 `CONTEXT_PACK_INPUT_DIGEST_MISMATCH`、非 READY/
CONSUMED（FAILED/INVALIDATED_BEFORE_USE/ASSEMBLING）`CONTEXT_PACK_NOT_READY`；以及放行路径——READY
首次消费 FRESH_CONSUMED、已 CONSUMED 幂等复用 RETRY_REUSED。

真实 PG（`KNOWLEDGE_TEST_DATABASE_URL` 未配置时跳过）：以真实 `SqlAlchemyContextPackRepository`
落 READY 头后，`consume_context_pack` 首次消费 FRESH_CONSUMED 并落 `consumed_at`、重复消费
RETRY_REUSED 幂等、hash/digest 不匹配失败关闭。零 LLM/零 I/O。
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4

from dev06_pg_support import TEST_DATABASE_URL, IsolatedDatabase, seed_foundation

from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.attempt_models import ContextPackRecord
from knowledge_system.infrastructure.persistence.context_pack_repository import (
    SqlAlchemyContextPackRepository,
)
from knowledge_system.modules.memory.public import (
    ContextPackReuseHitV1,
    ContextPackStateV1,
    ContextPackTransitionOutcomeV1,
    ContextPackTransitionResultV1,
    ContextPackV1,
    MemoryVersionSnapshotV1,
)
from knowledge_system.modules.model_gateway.application.context_pack_consumption import (
    ContextPackConsumeOutcomeV1,
    consume_context_pack,
)
from knowledge_system.modules.model_gateway.domain.controlled_call import ControlledCallFailure
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    ExactTokenAccountingV1,
)

_NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
_PACK_ID = uuid4()
_TASK_ID = uuid4()
_ACTOR = uuid4()
_BODY = uuid4()
PACK_HASH = "a" * 64
PACK_INPUT_DIGEST = "b" * 64


def _hit(**overrides: object) -> ContextPackReuseHitV1:
    defaults: dict[str, object] = {
        "id": _PACK_ID,
        "task_id": _TASK_ID,
        "state": ContextPackStateV1.READY,
        "pack_hash": PACK_HASH,
        "input_digest": PACK_INPUT_DIGEST,
        "input_tokens": 100,
        "body_artifact_id": _BODY,
        "created_at": _NOW,
        "consumed_at": None,
    }
    defaults.update(overrides)
    return ContextPackReuseHitV1(**defaults)


class _FakeContextPacks:
    """假 `ContextPackRepositoryPort`：固定返回头行，记录 `mark_consumed` 调用。"""

    def __init__(
        self,
        header: ContextPackReuseHitV1 | None,
        transition_outcome: ContextPackTransitionOutcomeV1 = (
            ContextPackTransitionOutcomeV1.TRANSITIONED
        ),
    ) -> None:
        self._header = header
        self._transition_outcome = transition_outcome
        self.consumed_calls: list[tuple[UUID, datetime]] = []

    async def find_by_id(self, *, pack_id: UUID) -> ContextPackReuseHitV1 | None:
        return self._header

    async def mark_consumed(
        self, *, pack_id: UUID, consumed_at: datetime
    ) -> ContextPackTransitionResultV1:
        self.consumed_calls.append((pack_id, consumed_at))
        return ContextPackTransitionResultV1(
            state=ContextPackStateV1.CONSUMED, outcome=self._transition_outcome
        )

    async def save_pack(self, *, pack: ContextPackV1) -> object:
        del pack
        raise NotImplementedError

    async def find_reusable_by_input_digest(
        self, *, task_id: UUID, input_digest: str
    ) -> object:
        del task_id, input_digest
        raise NotImplementedError

    async def invalidate_before_use(self, *, pack_id: UUID) -> object:
        del pack_id
        raise NotImplementedError


async def _consume(context_packs) -> None:
    await consume_context_pack(
        context_packs,
        context_pack_id=_PACK_ID,
        context_pack_hash=PACK_HASH,
        context_pack_input_digest=PACK_INPUT_DIGEST,
        consumed_at=_NOW,
    )


class ContextPackConsumptionGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_not_found_fails_closed(self) -> None:
        fake = _FakeContextPacks(header=None)
        with self.assertRaises(ControlledCallFailure) as ctx:
            await _consume(fake)
        self.assertEqual(ctx.exception.code, "CONTEXT_PACK_NOT_FOUND")
        self.assertEqual(fake.consumed_calls, [])

    async def test_hash_mismatch_fails_closed(self) -> None:
        fake = _FakeContextPacks(header=_hit(pack_hash="f" * 64))
        with self.assertRaises(ControlledCallFailure) as ctx:
            await _consume(fake)
        self.assertEqual(ctx.exception.code, "CONTEXT_PACK_HASH_MISMATCH")
        self.assertEqual(fake.consumed_calls, [])

    async def test_input_digest_mismatch_fails_closed(self) -> None:
        fake = _FakeContextPacks(header=_hit(input_digest="f" * 64))
        with self.assertRaises(ControlledCallFailure) as ctx:
            await _consume(fake)
        self.assertEqual(ctx.exception.code, "CONTEXT_PACK_INPUT_DIGEST_MISMATCH")
        self.assertEqual(fake.consumed_calls, [])

    async def test_non_ready_states_fail_closed(self) -> None:
        for state in (
            ContextPackStateV1.FAILED,
            ContextPackStateV1.INVALIDATED_BEFORE_USE,
            ContextPackStateV1.ASSEMBLING,
        ):
            with self.subTest(state=state.value):
                fake = _FakeContextPacks(header=_hit(state=state))
                with self.assertRaises(ControlledCallFailure) as ctx:
                    await _consume(fake)
                self.assertEqual(ctx.exception.code, "CONTEXT_PACK_NOT_READY")
                self.assertEqual(fake.consumed_calls, [])

    async def test_ready_transitions_to_fresh_consumed(self) -> None:
        fake = _FakeContextPacks(header=_hit(state=ContextPackStateV1.READY))
        result = await consume_context_pack(
            fake,
            context_pack_id=_PACK_ID,
            context_pack_hash=PACK_HASH,
            context_pack_input_digest=PACK_INPUT_DIGEST,
            consumed_at=_NOW,
        )
        self.assertEqual(result.outcome, ContextPackConsumeOutcomeV1.FRESH_CONSUMED)
        self.assertEqual(result.pack_id, _PACK_ID)
        self.assertEqual(result.state, ContextPackStateV1.CONSUMED)
        self.assertEqual(fake.consumed_calls, [(_PACK_ID, _NOW)])

    async def test_consumed_is_idempotent_retry_reused(self) -> None:
        fake = _FakeContextPacks(
            header=_hit(state=ContextPackStateV1.CONSUMED, consumed_at=_NOW),
            transition_outcome=ContextPackTransitionOutcomeV1.IDEMPOTENT,
        )
        result = await consume_context_pack(
            fake,
            context_pack_id=_PACK_ID,
            context_pack_hash=PACK_HASH,
            context_pack_input_digest=PACK_INPUT_DIGEST,
            consumed_at=_NOW,
        )
        self.assertEqual(result.outcome, ContextPackConsumeOutcomeV1.RETRY_REUSED)
        self.assertEqual(result.state, ContextPackStateV1.CONSUMED)


def _exact_accounting() -> ExactTokenAccountingV1:
    return ExactTokenAccountingV1(
        mode="EXACT",
        estimated=False,
        exact_input_tokens=1000,
        tokenizer_revision="tok-v1",
        chat_template_hash="a" * 64,
    )


def _pack(**overrides: object) -> ContextPackV1:
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
        "max_context_tokens": 32768,
        "reserved_output_tokens": 2048,
        "protocol_overhead_tokens": 512,
        "safety_margin_tokens": 128,
        "input_tokens": 1000,
        "items": (),
        "body_artifact_id": _BODY,
        "pack_hash": PACK_HASH,
        "input_digest": PACK_INPUT_DIGEST,
        "state": ContextPackStateV1.READY,
        "created_at": _NOW,
    }
    defaults.update(overrides)
    return ContextPackV1(**defaults)


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class ContextPackConsumptionPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev08_ctx_pack_consume_{os.getpid()}_{uuid4().hex[:8]}"
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

    def _pack_for(
        self, ids: dict[str, UUID], artifacts: dict[str, UUID], **overrides: object
    ) -> ContextPackV1:
        return _pack(
            id=uuid4(),
            task_id=ids["task"],
            actor_id=ids["user"],
            body_artifact_id=artifacts["query"],
            **overrides,
        )

    async def test_consume_fresh_transitions_and_persists_consumed_at(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts)
        async with self.session_factory() as session:
            await SqlAlchemyContextPackRepository(session).save_pack(pack=pack)
            await session.commit()

        async with self.session_factory() as session:
            repo = SqlAlchemyContextPackRepository(session)
            result = await consume_context_pack(
                repo,
                context_pack_id=pack.id,
                context_pack_hash=PACK_HASH,
                context_pack_input_digest=PACK_INPUT_DIGEST,
                consumed_at=_NOW,
            )
            await session.commit()

        self.assertEqual(result.outcome, ContextPackConsumeOutcomeV1.FRESH_CONSUMED)
        self.assertEqual(result.state, ContextPackStateV1.CONSUMED)
        async with self.session_factory() as session:
            header = await session.get(ContextPackRecord, pack.id)
            assert header is not None
            self.assertEqual(header.state, "CONSUMED")
            self.assertEqual(header.consumed_at, _NOW)

    async def test_consume_twice_is_idempotent_retry(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts)
        async with self.session_factory() as session:
            await SqlAlchemyContextPackRepository(session).save_pack(pack=pack)
            await session.commit()

        async with self.session_factory() as session:
            repo = SqlAlchemyContextPackRepository(session)
            first = await consume_context_pack(
                repo,
                context_pack_id=pack.id,
                context_pack_hash=PACK_HASH,
                context_pack_input_digest=PACK_INPUT_DIGEST,
                consumed_at=_NOW,
            )
            second = await consume_context_pack(
                repo,
                context_pack_id=pack.id,
                context_pack_hash=PACK_HASH,
                context_pack_input_digest=PACK_INPUT_DIGEST,
                consumed_at=_NOW,
            )
            await session.commit()

        self.assertEqual(first.outcome, ContextPackConsumeOutcomeV1.FRESH_CONSUMED)
        self.assertEqual(second.outcome, ContextPackConsumeOutcomeV1.RETRY_REUSED)

    async def test_consume_hash_mismatch_fails_closed(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts, pack_hash="f" * 64)
        async with self.session_factory() as session:
            await SqlAlchemyContextPackRepository(session).save_pack(pack=pack)
            await session.commit()

        async with self.session_factory() as session:
            with self.assertRaises(ControlledCallFailure) as ctx:
                await consume_context_pack(
                    SqlAlchemyContextPackRepository(session),
                    context_pack_id=pack.id,
                    context_pack_hash=PACK_HASH,
                    context_pack_input_digest=PACK_INPUT_DIGEST,
                    consumed_at=_NOW,
                )
            self.assertEqual(ctx.exception.code, "CONTEXT_PACK_HASH_MISMATCH")


if __name__ == "__main__":
    unittest.main()
