"""DEV-05 批2：组合根 `compose_model_gateway_service` 真实PG端到端验收。

以真实`ModelCallLedgerRepository`/`BudgetLedgerRepository`/`PostgresEgressPermitAuthority` +
固定Secret版本解析器 + 假Bifrost流，验证`ModelGatewayService.complete()`在显式提交边界下的
完整生命周期：ACCEPTED→SENDING（提交）→签发READY许可→流→COMPLETED（提交）+ 预算结算。
SecretVersion以固定解析器注入、Bifrost以假流注入，不接真实OpenBao/Bifrost/模型。
"""

from __future__ import annotations

import asyncio
import os
import unittest
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy import event

from knowledge_system.infrastructure.model_gateway.composition import compose_model_gateway_service
from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    ModelCallLedgerRepository,
    PostgresEgressPermitAuthority,
    SqlAlchemyContextPackRepository,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.modules.memory.public import (
    ContextPackStateV1,
    ContextPackV1,
    MemoryVersionSnapshotV1,
)
from knowledge_system.modules.model_gateway.domain.budget_quantity import BudgetQuantityV1
from knowledge_system.modules.model_gateway.domain.call_state import ModelCallState
from knowledge_system.modules.model_gateway.domain.chat_request import (
    ChatJsonSchemaV1,
    ChatMessageV1,
)
from knowledge_system.modules.model_gateway.domain.controlled_call import ControlledCallPlanV1
from knowledge_system.modules.model_gateway.domain.egress_permit import EgressPermitStatus
from knowledge_system.modules.model_gateway.domain.stream_event import (
    BifrostDeltaEventV1,
    BifrostEventV1,
    BifrostUsageEventV1,
)
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    EstimatedDevTokenAccountingV1,
)

TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")
SERVER_ROOT = Path(__file__).resolve().parents[1]
IDENTITY_HMAC_KEY = b"k" * 32
WORKSPACE_ID = "ws-test123"
PACK_HASH = "a" * 64
PACK_INPUT_DIGEST = "b" * 64


class _FixedVersionResolver:
    async def current_version(self, secret_path: str) -> int:
        del secret_path
        return 1


class _FakeBifrost:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def stream(
        self, *, model_call_id, trace_id, route_ref, request
    ) -> AsyncIterator[BifrostEventV1]:
        self.calls.append(request)

        async def generate() -> AsyncIterator[BifrostEventV1]:
            yield BifrostDeltaEventV1(type="DELTA", text_delta='{"goal":"answer"}', chunk_id="c1")
            yield BifrostUsageEventV1(type="USAGE", input_tokens=10, output_tokens=5)

        return generate()


def _plan() -> ControlledCallPlanV1:
    return ControlledCallPlanV1(
        model_call_id=uuid4(),
        task_id=uuid4(),
        account_id=uuid4(),
        context_pack_id=uuid4(),
        context_pack_hash=PACK_HASH,
        context_pack_input_digest=PACK_INPUT_DIGEST,
        capability_alias="planning_strong",
        messages=(ChatMessageV1(role="user", content="public development input"),),
        max_tokens=128,
        response_schema=ChatJsonSchemaV1(
            name="goal_answer",
            schema={
                "type": "object",
                "properties": {"goal": {"type": "string"}},
                "required": ["goal"],
                "additionalProperties": False,
            },
        ),
        token_accounting=EstimatedDevTokenAccountingV1(
            mode="ESTIMATED_DEV",
            estimated=True,
            estimated_input_tokens=64,
            estimator_revision="est-1",
            serialized_request_bytes=1024,
            protocol_overhead_tokens=8,
        ),
        reserved=BudgetQuantityV1(model_call_count=1, input_tokens=100, output_tokens=100),
        prompt_version="v1",
        context_policy_version="v1",
        runtime_version="v1",
        agent_manifest_version="v1",
        authorization_version=1,
        execution_generation=1,
        cancellation_generation=0,
        expires_in_seconds=300,
    )


def _pack_for(plan: ControlledCallPlanV1) -> ContextPackV1:
    """落一个与 plan 三件套匹配的 READY Context Pack（FK 已由 `_disable_fk` 旁路）。"""
    return ContextPackV1(
        id=plan.context_pack_id,
        task_id=plan.task_id,
        actor_id=uuid4(),
        policy_version="ctx-policy-v1",
        memory_versions=MemoryVersionSnapshotV1(),
        runtime_version="rt-v1",
        agent_manifest_version="am-v1",
        model_revision="model-v1",
        prompt_version="v1",
        token_accounting=EstimatedDevTokenAccountingV1(
            mode="ESTIMATED_DEV",
            estimated=True,
            estimated_input_tokens=64,
            estimator_revision="est-1",
            serialized_request_bytes=1024,
            protocol_overhead_tokens=8,
        ),
        max_context_tokens=32768,
        reserved_output_tokens=2048,
        protocol_overhead_tokens=512,
        safety_margin_tokens=128,
        input_tokens=64,
        items=(),
        body_artifact_id=uuid4(),
        pack_hash=PACK_HASH,
        input_digest=PACK_INPUT_DIGEST,
        state=ContextPackStateV1.READY,
        created_at=datetime.now(UTC),
    )


def _pg_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    return url


def _isolated_url(base_url: str, dbname: str) -> str:
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def _alembic_config() -> Config:
    return Config(str(SERVER_ROOT / "alembic.ini"))


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class ModelGatewayCompositionPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev05_composition_{os.getpid()}_{uuid4().hex[:8]}"
        self.isolated_url = _isolated_url(TEST_DATABASE_URL, self.dbname)
        with psycopg.connect(_pg_url(TEST_DATABASE_URL), autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{self.dbname}"')
        previous_url = os.environ.get("KNOWLEDGE_DATABASE_URL")
        os.environ["KNOWLEDGE_DATABASE_URL"] = self.isolated_url
        try:
            await asyncio.to_thread(command.upgrade, _alembic_config(), "head")
        finally:
            if previous_url is None:
                os.environ.pop("KNOWLEDGE_DATABASE_URL", None)
            else:
                os.environ["KNOWLEDGE_DATABASE_URL"] = previous_url
        self.engine = build_async_engine(DatabaseEngineSettings(url=self.isolated_url))
        # 本端到端测试只聚焦受控调用链，绕过无关冻结FK（task/context_pack等），沿用既有测试手法。
        event.listen(self.engine.sync_engine, "connect", _disable_fk)
        self.session_factory = build_session_factory(self.engine)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        assert TEST_DATABASE_URL is not None
        with psycopg.connect(_pg_url(TEST_DATABASE_URL), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)')

    async def test_complete_end_to_end_commits_pre_and_post_stream(self) -> None:
        bifrost = _FakeBifrost()
        wiring = compose_model_gateway_service(
            session_factory=self.session_factory,
            permit_authority=PostgresEgressPermitAuthority(self.session_factory),
            version_resolver=_FixedVersionResolver(),
            bifrost=bifrost,
            identity_hmac_key=IDENTITY_HMAC_KEY,
            workspace_id=WORKSPACE_ID,
        )
        plan = _plan()
        async with self.session_factory() as session:
            await SqlAlchemyContextPackRepository(session).save_pack(pack=_pack_for(plan))
            await session.commit()

        async with wiring.transaction:
            result = await wiring.service.complete(plan)

        self.assertEqual(result.status, ModelCallState.COMPLETED)
        self.assertEqual(result.text, '{"goal":"answer"}')
        self.assertEqual(result.usage.kind, "EXACT")
        self.assertEqual(result.usage.input_tokens, 10)
        self.assertEqual(result.usage.output_tokens, 5)
        self.assertEqual(len(bifrost.calls), 1)

        async with self.session_factory() as session:
            call = await ModelCallLedgerRepository(session).get_by_id(plan.model_call_id)
        self.assertIsNotNone(call)
        assert call is not None
        self.assertEqual(call.status, ModelCallState.COMPLETED)
        self.assertEqual(call.approved_model_id, "qwen3.8-max-0902")

        permit = await PostgresEgressPermitAuthority(self.session_factory).get(plan.model_call_id)
        self.assertIsNotNone(permit)
        assert permit is not None
        self.assertEqual(permit.status, EgressPermitStatus.READY)
        self.assertEqual(permit.secret_version, 1)
        self.assertTrue(permit.secret_ref.startswith("openbao-kv-v2://knowledge-model/model-provider/"))


def _disable_fk(dbapi_connection: object, connection_record: object) -> None:
    del connection_record
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("SET session_replication_role = replica")
    cursor.close()


if __name__ == "__main__":
    unittest.main()
