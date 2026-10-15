"""Real PostgreSQL acceptance for one-shot egress admission and Broker authorization."""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    EgressPermitIssueDisposition,
    ModelCallLedgerRepository,
    PostgresEgressPermitAuthority,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.model_call_repository import record_from_v1
from knowledge_system.modules.model_gateway.application.egress import (
    EgressAdmissionOutcome,
    EgressAdmissionRequestV1,
)
from knowledge_system.modules.model_gateway.application.gateway import ModelCallTransitionUpdates
from knowledge_system.modules.model_gateway.application.secrets import ProviderSecretReadRequestV1
from knowledge_system.modules.model_gateway.domain.call_state import ModelCallState
from knowledge_system.modules.model_gateway.domain.egress_admission import EgressBodyDigestV1
from knowledge_system.modules.model_gateway.domain.egress_permit import (
    EgressPermitStatus,
    EgressPermitV1,
)
from knowledge_system.modules.model_gateway.domain.model_call import DataScope, ModelCallV1
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    ExactTokenAccountingV1,
)
from knowledge_system.modules.model_gateway.domain.usage import UnknownModelUsageV1

TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")
SERVER_ROOT = Path(__file__).resolve().parents[1]
DIGEST = "a" * 64
SECRET_REF = "openbao-kv-v2://knowledge-model/model-provider/" + "b" * 64


def _pg_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    return url


def _isolated_url(base_url: str, dbname: str) -> str:
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def _alembic_config() -> Config:
    return Config(str(SERVER_ROOT / "alembic.ini"))


def _call(model_call_id, context_pack_id) -> ModelCallV1:
    return ModelCallV1(
        model_call_id=model_call_id,
        context_pack_id=context_pack_id,
        context_pack_hash="a" * 64,
        context_pack_input_digest="b" * 64,
        execution_generation=5,
        cancellation_generation=2,
        capability_alias="planning_strong",
        approved_model_id="qwen-plus",
        approved_model_revision="qwen-plus",
        prompt_version="goal-plan-v1",
        context_policy_version="context-policy-v1",
        authorization_version=3,
        runtime_version="runtime-v1",
        agent_manifest_version="supervisor-v1",
        conversation_memory_version=0,
        user_private_memory_version=0,
        task_memory_version=0,
        token_accounting=ExactTokenAccountingV1(
            mode="EXACT",
            estimated=False,
            exact_input_tokens=32,
            tokenizer_revision="qwen-tokenizer-v1",
            chat_template_hash=DIGEST,
        ),
        data_scope=DataScope.PUBLIC_DEVELOPMENT,
        request_hash="c" * 64,
        usage=UnknownModelUsageV1(kind="UNKNOWN"),
        status=ModelCallState.ACCEPTED,
        created_at=datetime.now(UTC),
    )


def _permit(model_call_id, body: EgressBodyDigestV1) -> EgressPermitV1:
    now = datetime.now(UTC)
    return EgressPermitV1(
        model_call_id=model_call_id,
        route_ref="route/planning-strong/v1",
        provider="alibaba_bailian",
        provider_model="qwen-plus",
        secret_ref=SECRET_REF,
        secret_version=1,
        body=body,
        authorization_version=3,
        execution_generation=5,
        cancellation_generation=2,
        status=EgressPermitStatus.READY,
        expires_at=now + timedelta(minutes=2),
        row_version=1,
        created_at=now,
    )


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class Dev04EgressPermitPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev04_egress_{os.getpid()}_{uuid4().hex[:8]}"
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
        self.session_factory = build_session_factory(self.engine)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        assert TEST_DATABASE_URL is not None
        with psycopg.connect(_pg_url(TEST_DATABASE_URL), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)')

    async def test_permit_is_idempotent_and_each_runtime_gate_is_one_shot(self) -> None:
        model_call_id = uuid4()
        call = _call(model_call_id, uuid4())
        # This focused test bypasses only unrelated ContextPack seed FKs in its disposable DB.
        async with self.session_factory() as session, session.begin():
            await session.execute(text("SET LOCAL session_replication_role = replica"))
            session.add(record_from_v1(call))
        async with self.session_factory() as session, session.begin():
            transition = await ModelCallLedgerRepository(session).transition_status(
                model_call_id,
                from_status=ModelCallState.ACCEPTED,
                to_status=ModelCallState.SENDING,
                expected_row_version=1,
                updates=ModelCallTransitionUpdates(),
            )
            self.assertTrue(transition.committed)

        body = EgressBodyDigestV1(
            model_id="qwen-plus",
            model_revision="qwen-plus",
            parameters_digest=DIGEST,
            schema_digest="d" * 64,
            input_digest="e" * 64,
        )
        authority = PostgresEgressPermitAuthority(self.session_factory)
        permit = _permit(model_call_id, body)
        self.assertEqual(
            await authority.issue_ready(permit),
            EgressPermitIssueDisposition.INSERTED,
        )
        self.assertEqual(
            await authority.issue_ready(permit),
            EgressPermitIssueDisposition.IDEMPOTENT_REPLAY,
        )

        request = EgressAdmissionRequestV1(
            model_call_id=model_call_id,
            route_ref=permit.route_ref,
            body=body,
        )
        decisions = await asyncio.gather(
            authority.admit(request=request),
            authority.admit(request=request),
        )
        self.assertEqual(
            sorted(decision.outcome for decision in decisions),
            [EgressAdmissionOutcome.ADMITTED, EgressAdmissionOutcome.REJECTED],
        )

        secret_request = ProviderSecretReadRequestV1(
            model_call_id=model_call_id,
            route_ref=permit.route_ref,
            secret_ref=permit.secret_ref,
            secret_version=permit.secret_version,
        )
        authorizations = await asyncio.gather(
            authority.authorize(secret_request),
            authority.authorize(secret_request),
        )
        self.assertEqual(sorted(authorizations), [False, True])
        stored = await authority.get(model_call_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.status, EgressPermitStatus.SECRET_READ)
        self.assertEqual(stored.row_version, 3)


if __name__ == "__main__":
    unittest.main()
