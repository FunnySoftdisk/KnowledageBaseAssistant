"""DEV-05 批2：出网许可签发方（`PostgresEgressPermitIssuer`）离线与真实PG验收。

离线：以Fake Authority/版本解析器验证SecretRef HMAC派生、SecretVersion解析、`EgressPermitV1`
映射与签发结果（INSERTED/IDEMPOTENT_REPLAY）映射，并验证签发结果不回传SecretRef/SecretVersion。
真实PG：以真实`PostgresEgressPermitAuthority`验证READY许可落库、SecretRef/版本与冻结Body一致。
不接真实OpenBao；版本号以固定解析器注入。
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from knowledge_system.infrastructure.model_gateway.egress_permit_issuer import (
    OpenBaoMetadataVersionResolver,
    PostgresEgressPermitIssuer,
)
from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    EgressPermitIssueDisposition,
    ModelCallLedgerRepository,
    PostgresEgressPermitAuthority,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.egress_permit_repository import (
    EgressPermitContractError,
)
from knowledge_system.infrastructure.persistence.model_call_repository import record_from_v1
from knowledge_system.modules.model_gateway.application.egress_permit_issuance import (
    EgressPermitIssuanceDisposition,
    EgressPermitIssuanceRequestV1,
)
from knowledge_system.modules.model_gateway.application.gateway import ModelCallTransitionUpdates
from knowledge_system.modules.model_gateway.domain.call_state import ModelCallState
from knowledge_system.modules.model_gateway.domain.egress_admission import EgressBodyDigestV1
from knowledge_system.modules.model_gateway.domain.egress_permit import (
    EgressPermitStatus,
    EgressPermitV1,
)
from knowledge_system.modules.model_gateway.domain.model_call import DataScope, ModelCallV1
from knowledge_system.modules.model_gateway.domain.secret_reference import (
    derive_provider_secret_path,
)
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    ExactTokenAccountingV1,
)
from knowledge_system.modules.model_gateway.domain.usage import UnknownModelUsageV1

TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")
SERVER_ROOT = Path(__file__).resolve().parents[1]
DIGEST = "a" * 64
IDENTITY_HMAC_KEY = b"k" * 32
WORKSPACE_ID = "ws-test123"
PROVIDER = "alibaba_bailian"
REGION = "cn-beijing"
MOUNT = "knowledge-model"


class _FixedVersionResolver:
    def __init__(self, version: int = 1) -> None:
        self.version = version
        self.paths: list[str] = []

    async def current_version(self, secret_path: str) -> int:
        self.paths.append(secret_path)
        return self.version


class _FakeAuthority:
    def __init__(
        self,
        disposition: EgressPermitIssueDisposition = EgressPermitIssueDisposition.INSERTED,
    ) -> None:
        self.disposition = disposition
        self.permits: list[EgressPermitV1] = []

    async def issue_ready(self, permit: EgressPermitV1) -> EgressPermitIssueDisposition:
        self.permits.append(permit)
        return self.disposition


def _issuer(version_resolver, authority) -> PostgresEgressPermitIssuer:
    return PostgresEgressPermitIssuer(
        authority=authority,
        version_resolver=version_resolver,
        identity_hmac_key=IDENTITY_HMAC_KEY,
        workspace_id=WORKSPACE_ID,
        provider=PROVIDER,
        region=REGION,
        mount=MOUNT,
    )


def _request(
    *,
    model_call_id: UUID | None = None,
    expires_at: datetime | None = None,
    provider_model: str = "qwen3.8-max-0902",
    authorization_version: int = 1,
    execution_generation: int = 1,
    cancellation_generation: int = 0,
) -> EgressPermitIssuanceRequestV1:
    return EgressPermitIssuanceRequestV1(
        model_call_id=model_call_id or uuid4(),
        route_ref="development/planning_strong",
        provider=PROVIDER,
        provider_model=provider_model,
        body=EgressBodyDigestV1(
            model_id=provider_model,
            model_revision=provider_model,
            parameters_digest="b" * 64,
            schema_digest="c" * 64,
            input_digest="d" * 64,
        ),
        authorization_version=authorization_version,
        execution_generation=execution_generation,
        cancellation_generation=cancellation_generation,
        expires_at=expires_at or (datetime.now(UTC) + timedelta(seconds=300)),
    )


class PostgresEgressPermitIssuerTests(unittest.IsolatedAsyncioTestCase):
    async def test_derives_secret_ref_and_maps_ready_permit(self) -> None:
        resolver = _FixedVersionResolver(version=5)
        authority = _FakeAuthority()
        issuer = _issuer(resolver, authority)
        request = _request()

        result = await issuer.issue_ready(request)

        self.assertEqual(result.disposition, EgressPermitIssuanceDisposition.INSERTED)
        self.assertEqual(result.model_dump(), {"disposition": "INSERTED"})

        expected_path = derive_provider_secret_path(
            identity_hmac_key=IDENTITY_HMAC_KEY,
            workspace_id=WORKSPACE_ID,
            provider=PROVIDER,
            region=REGION,
        )
        self.assertEqual(resolver.paths, [expected_path])

        permit = authority.permits[0]
        self.assertEqual(permit.status, EgressPermitStatus.READY)
        self.assertEqual(permit.row_version, 1)
        self.assertEqual(permit.secret_version, 5)
        self.assertEqual(permit.secret_ref, f"openbao-kv-v2://{MOUNT}/{expected_path}")
        self.assertEqual(permit.model_call_id, request.model_call_id)
        self.assertEqual(permit.route_ref, request.route_ref)
        self.assertEqual(permit.provider, request.provider)
        self.assertEqual(permit.provider_model, request.provider_model)
        self.assertEqual(permit.body, request.body)
        self.assertEqual(permit.authorization_version, request.authorization_version)
        self.assertEqual(permit.execution_generation, request.execution_generation)
        self.assertEqual(permit.cancellation_generation, request.cancellation_generation)
        self.assertEqual(permit.expires_at, request.expires_at)
        self.assertIsNone(permit.admitted_at)
        self.assertIsNone(permit.secret_read_at)

    async def test_maps_idempotent_replay_disposition(self) -> None:
        authority = _FakeAuthority(EgressPermitIssueDisposition.IDEMPOTENT_REPLAY)
        issuer = _issuer(_FixedVersionResolver(), authority)

        result = await issuer.issue_ready(_request())

        self.assertEqual(result.disposition, EgressPermitIssuanceDisposition.IDEMPOTENT_REPLAY)

    async def test_rejects_expired_permit(self) -> None:
        issuer = _issuer(_FixedVersionResolver(), _FakeAuthority())
        request = _request(expires_at=datetime.now(UTC) - timedelta(seconds=1))

        with self.assertRaisesRegex(EgressPermitContractError, "EGRESS_PERMIT_EXPIRY_INVALID"):
            await issuer.issue_ready(request)

    async def test_metadata_resolver_delegates_to_client(self) -> None:
        class _StubClient:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def read_current_version(self, *, secret_path: str) -> int:
                self.calls.append(secret_path)
                return 7

        client = _StubClient()
        resolver = OpenBaoMetadataVersionResolver(client)
        version = await resolver.current_version("model-provider/" + "a" * 64)
        self.assertEqual(version, 7)
        self.assertEqual(client.calls, ["model-provider/" + "a" * 64])


def _pg_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    return url


def _isolated_url(base_url: str, dbname: str) -> str:
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def _alembic_config() -> Config:
    return Config(str(SERVER_ROOT / "alembic.ini"))


def _call(model_call_id: UUID, context_pack_id: UUID) -> ModelCallV1:
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


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class PostgresEgressPermitIssuerPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev05_issuer_{os.getpid()}_{uuid4().hex[:8]}"
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

    async def test_issue_ready_persists_ready_permit_with_derived_secret_ref(self) -> None:
        model_call_id = uuid4()
        call = _call(model_call_id, uuid4())
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

        authority = PostgresEgressPermitAuthority(self.session_factory)
        resolver = _FixedVersionResolver(version=1)
        issuer = _issuer(resolver, authority)
        request = _request(
            model_call_id=model_call_id,
            provider_model="qwen-plus",
            authorization_version=call.authorization_version,
            execution_generation=call.execution_generation,
            cancellation_generation=call.cancellation_generation,
        )

        result = await issuer.issue_ready(request)

        self.assertEqual(result.disposition, EgressPermitIssuanceDisposition.INSERTED)
        expected_path = derive_provider_secret_path(
            identity_hmac_key=IDENTITY_HMAC_KEY,
            workspace_id=WORKSPACE_ID,
            provider=PROVIDER,
            region=REGION,
        )
        stored = await authority.get(model_call_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.status, EgressPermitStatus.READY)
        self.assertEqual(stored.row_version, 1)
        self.assertEqual(stored.secret_version, 1)
        self.assertEqual(stored.secret_ref, f"openbao-kv-v2://{MOUNT}/{expected_path}")
        self.assertEqual(stored.provider_model, "qwen-plus")
        self.assertEqual(stored.authorization_version, call.authorization_version)
        self.assertEqual(stored.execution_generation, call.execution_generation)
        self.assertEqual(stored.cancellation_generation, call.cancellation_generation)


if __name__ == "__main__":
    unittest.main()
