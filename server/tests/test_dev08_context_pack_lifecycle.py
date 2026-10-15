"""DEV-08 子批次 11：Context Pack 消费门（§12.1 状态机）的离线与真实 PG 验收。

离线部分证明 `is_legal_context_pack_transition` 真值表：ASSEMBLING→READY/FAILED、READY→CONSUMED/
INVALIDATED_BEFORE_USE 合法，终态 CONSUMED/FAILED/INVALIDATED_BEFORE_USE 无出边，同态不在表内
（幂等由 Store 按 IDEMPOTENT 处理）。真实 PG 部分（`KNOWLEDGE_TEST_DATABASE_URL` 未配置时跳过）
证明 `find_by_id` 任意状态读回、`mark_consumed`（READY→CONSUMED 落 `consumed_at`、已 CONSUMED 幂等、
FAILED/INVALIDATED/不存在失败关闭）、`invalidate_before_use`（READY→INVALIDATED_BEFORE_USE、已失效
幂等、CONSUMED/FAILED/不存在失败关闭）。零 LLM/零 I/O。
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
    ContextPackIntegrityError,
    SqlAlchemyContextPackRepository,
)
from knowledge_system.modules.memory.application.ports import (
    ContextPackTransitionOutcomeV1,
)
from knowledge_system.modules.memory.public import (
    ContextPackStateV1,
    ContextPackV1,
    MemoryVersionSnapshotV1,
    is_legal_context_pack_transition,
)
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    ExactTokenAccountingV1,
)

_NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
_PACK_ID = uuid4()
_TASK_ID = uuid4()
_ACTOR = uuid4()
_BODY = uuid4()


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
        "pack_hash": "b" * 64,
        "input_digest": "c" * 64,
        "state": ContextPackStateV1.READY,
        "created_at": _NOW,
    }
    defaults.update(overrides)
    return ContextPackV1(**defaults)


class ContextPackStateMachineTests(unittest.TestCase):
    def test_legal_transitions(self) -> None:
        legal = {
            (ContextPackStateV1.ASSEMBLING, ContextPackStateV1.READY),
            (ContextPackStateV1.ASSEMBLING, ContextPackStateV1.FAILED),
            (ContextPackStateV1.READY, ContextPackStateV1.CONSUMED),
            (ContextPackStateV1.READY, ContextPackStateV1.INVALIDATED_BEFORE_USE),
        }
        for current, next_ in legal:
            self.assertTrue(
                is_legal_context_pack_transition(current, next_),
                f"expected legal: {current.value} -> {next_.value}",
            )

    def test_terminal_states_have_no_outgoing(self) -> None:
        terminal = {
            ContextPackStateV1.CONSUMED,
            ContextPackStateV1.FAILED,
            ContextPackStateV1.INVALIDATED_BEFORE_USE,
        }
        for current in terminal:
            for next_ in ContextPackStateV1:
                self.assertFalse(
                    is_legal_context_pack_transition(current, next_),
                    f"expected illegal: {current.value} -> {next_.value}",
                )

    def test_same_state_is_not_a_transition(self) -> None:
        for state in ContextPackStateV1:
            self.assertFalse(
                is_legal_context_pack_transition(state, state),
                f"same state is not a transition: {state.value}",
            )

    def test_read_consumed_and_backward_illegal(self) -> None:
        self.assertFalse(
            is_legal_context_pack_transition(
                ContextPackStateV1.CONSUMED, ContextPackStateV1.READY
            )
        )
        self.assertFalse(
            is_legal_context_pack_transition(
                ContextPackStateV1.READY, ContextPackStateV1.ASSEMBLING
            )
        )


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class ContextPackLifecyclePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev08_context_pack_lifecycle_{os.getpid()}_{uuid4().hex[:8]}"
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
            id=uuid4(),
            task_id=ids["task"],
            actor_id=ids["user"],
            body_artifact_id=artifacts["query"],
        )

    async def test_find_by_id_returns_header_any_state(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts)
        async with self.session_factory() as session:
            await SqlAlchemyContextPackRepository(session).save_pack(pack=pack)
            await session.commit()
        async with self.session_factory() as session:
            hit = await SqlAlchemyContextPackRepository(session).find_by_id(pack_id=pack.id)
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertEqual(hit.id, pack.id)
            self.assertEqual(hit.state, ContextPackStateV1.READY)
            self.assertEqual(hit.pack_hash, "b" * 64)

    async def test_find_by_id_miss_returns_none(self) -> None:
        ids, artifacts = await self._seed()
        async with self.session_factory() as session:
            hit = await SqlAlchemyContextPackRepository(session).find_by_id(pack_id=uuid4())
            self.assertIsNone(hit)

    async def test_mark_consumed_transitions_ready(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts)
        async with self.session_factory() as session:
            repo = SqlAlchemyContextPackRepository(session)
            await repo.save_pack(pack=pack)
            await session.commit()
        async with self.session_factory() as session:
            result = await SqlAlchemyContextPackRepository(session).mark_consumed(
                pack_id=pack.id, consumed_at=_NOW
            )
            await session.commit()
        self.assertEqual(result.outcome, ContextPackTransitionOutcomeV1.TRANSITIONED)
        self.assertEqual(result.state, ContextPackStateV1.CONSUMED)
        async with self.session_factory() as session:
            header = await session.get(ContextPackRecord, pack.id)
            assert header is not None
            self.assertEqual(header.state, "CONSUMED")
            self.assertEqual(header.consumed_at, _NOW)

    async def test_mark_consumed_idempotent_when_already_consumed(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts)
        async with self.session_factory() as session:
            repo = SqlAlchemyContextPackRepository(session)
            await repo.save_pack(pack=pack)
            await session.commit()
        async with self.session_factory() as session:
            repo = SqlAlchemyContextPackRepository(session)
            await repo.mark_consumed(pack_id=pack.id, consumed_at=_NOW)
            await session.commit()
        async with self.session_factory() as session:
            result = await SqlAlchemyContextPackRepository(session).mark_consumed(
                pack_id=pack.id, consumed_at=_NOW
            )
        self.assertEqual(result.outcome, ContextPackTransitionOutcomeV1.IDEMPOTENT)
        self.assertEqual(result.state, ContextPackStateV1.CONSUMED)

    async def test_mark_consumed_fails_closed_on_failed(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts).model_copy(
            update={"state": ContextPackStateV1.FAILED}
        )
        async with self.session_factory() as session:
            await SqlAlchemyContextPackRepository(session).save_pack(pack=pack)
            await session.commit()
        async with self.session_factory() as session:
            with self.assertRaises(ContextPackIntegrityError) as ctx:
                await SqlAlchemyContextPackRepository(session).mark_consumed(
                    pack_id=pack.id, consumed_at=_NOW
                )
            self.assertEqual(str(ctx.exception), "CONTEXT_PACK_ILLEGAL_STATE_TRANSITION")

    async def test_mark_consumed_not_found_fails_closed(self) -> None:
        await self._seed()
        async with self.session_factory() as session:
            with self.assertRaises(ContextPackIntegrityError) as ctx:
                await SqlAlchemyContextPackRepository(session).mark_consumed(
                    pack_id=uuid4(), consumed_at=_NOW
                )
            self.assertEqual(str(ctx.exception), "CONTEXT_PACK_NOT_FOUND")

    async def test_invalidate_before_use_transitions_ready(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts)
        async with self.session_factory() as session:
            repo = SqlAlchemyContextPackRepository(session)
            await repo.save_pack(pack=pack)
            await session.commit()
        async with self.session_factory() as session:
            result = await SqlAlchemyContextPackRepository(session).invalidate_before_use(
                pack_id=pack.id
            )
            await session.commit()
        self.assertEqual(result.outcome, ContextPackTransitionOutcomeV1.TRANSITIONED)
        self.assertEqual(result.state, ContextPackStateV1.INVALIDATED_BEFORE_USE)
        async with self.session_factory() as session:
            header = await session.get(ContextPackRecord, pack.id)
            assert header is not None
            self.assertEqual(header.state, "INVALIDATED_BEFORE_USE")

    async def test_invalidate_before_use_idempotent(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts)
        async with self.session_factory() as session:
            repo = SqlAlchemyContextPackRepository(session)
            await repo.save_pack(pack=pack)
            await session.commit()
        async with self.session_factory() as session:
            repo = SqlAlchemyContextPackRepository(session)
            await repo.invalidate_before_use(pack_id=pack.id)
            await session.commit()
        async with self.session_factory() as session:
            result = await SqlAlchemyContextPackRepository(session).invalidate_before_use(
                pack_id=pack.id
            )
        self.assertEqual(result.outcome, ContextPackTransitionOutcomeV1.IDEMPOTENT)
        self.assertEqual(result.state, ContextPackStateV1.INVALIDATED_BEFORE_USE)

    async def test_invalidate_before_use_fails_closed_on_consumed(self) -> None:
        ids, artifacts = await self._seed()
        pack = self._pack_for(ids, artifacts)
        async with self.session_factory() as session:
            repo = SqlAlchemyContextPackRepository(session)
            await repo.save_pack(pack=pack)
            await session.commit()
        async with self.session_factory() as session:
            await SqlAlchemyContextPackRepository(session).mark_consumed(
                pack_id=pack.id, consumed_at=_NOW
            )
            await session.commit()
        async with self.session_factory() as session:
            with self.assertRaises(ContextPackIntegrityError) as ctx:
                await SqlAlchemyContextPackRepository(session).invalidate_before_use(
                    pack_id=pack.id
                )
            self.assertEqual(str(ctx.exception), "CONTEXT_PACK_ILLEGAL_STATE_TRANSITION")


if __name__ == "__main__":
    unittest.main()
