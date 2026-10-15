"""DEV-04 exit item: run one real paid controlled model call through the full chain.

Constructs a minimal `ControlledCallPlanV1` and runs `compose_model_gateway_service(...)`
with the REAL adapters: PostgreSQL (`knowledge` superuser role), OpenBao metadata
version resolution (mTLS + single-use token) and the Bifrost HTTP data plane (mTLS).

This is a one-shot development validation, not the production runtime: it connects as
the `knowledge` database superuser and disables foreign-key enforcement for the frozen
`task`/`context_pack` references, mirroring the DEV-05 composition test's approach. It
seeds a READY `workflow.context_pack` header (id/`pack_hash`/`input_digest` lining up with
`build_plan`'s context-pack triple) so the consumption gate passes. The
plan carries no Secret, endpoint URL or Provider Key; those are derived inside the
issuer/broker exactly as in the running gateway chain.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from knowledge_system.infrastructure.model_gateway.bifrost import BifrostHttpClient
from knowledge_system.infrastructure.model_gateway.composition import (
    compose_model_gateway_service,
)
from knowledge_system.infrastructure.model_gateway.egress_permit_issuer import (
    OpenBaoMetadataVersionResolver,
)
from knowledge_system.infrastructure.model_gateway.openbao import (
    OpenBaoAgentFileTokenProvider,
    OpenBaoKv2Client,
)
from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    PostgresEgressPermitAuthority,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.modules.model_gateway.domain.budget_quantity import BudgetQuantityV1
from knowledge_system.modules.model_gateway.domain.chat_request import (
    ChatJsonSchemaV1,
    ChatMessageV1,
)
from knowledge_system.modules.model_gateway.domain.controlled_call import ControlledCallPlanV1
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    EstimatedDevTokenAccountingV1,
)

CAPABILITY_ALIAS = "planning_strong"
MAX_TOKENS = 128
DIGEST = "a" * 64


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name}_REQUIRED")
    return value


def _read_identity_key(path: Path) -> bytes:
    value = path.read_bytes()
    if len(value) != 32:
        raise RuntimeError("IDENTITY_HMAC_KEY_LENGTH_INVALID")
    return value


def build_plan(pack_id: UUID, task_id: UUID, account_id: UUID) -> ControlledCallPlanV1:
    """Build the minimal, public-development-only controlled call plan.

    `context_pack_id`/`context_pack_hash`/`context_pack_input_digest` 与 `_seed` 落库的
    `workflow.context_pack` 头一致（pack_hash/input_digest 均为 `DIGEST`），使消费门真实穿链。
    """
    return ControlledCallPlanV1(
        model_call_id=uuid4(),
        task_id=task_id,
        account_id=account_id,
        context_pack_id=pack_id,
        context_pack_hash=DIGEST,
        context_pack_input_digest=DIGEST,
        capability_alias=CAPABILITY_ALIAS,
        messages=(ChatMessageV1(role="user", content="Reply with the single word: ready"),),
        max_tokens=MAX_TOKENS,
        response_schema=ChatJsonSchemaV1(
            name="canary",
            schema={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
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


def _disable_fk(dbapi_connection: object, connection_record: object) -> None:
    del connection_record
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("SET session_replication_role = replica")
    cursor.close()


async def _seed(engine: AsyncEngine, ids: dict[str, UUID]) -> None:
    """Seed a READY `workflow.context_pack` header so the gateway consumption gate passes.

    FK enforcement is disabled; the task/actor/conversation/artifact UUIDs are opaque and only
    the pack header identity (`id`/`pack_hash`/`input_digest`) must line up with `build_plan`.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO workflow.context_pack "
                "(id,task_id,action_attempt_id,created_by_attempt_id,conversation_id,actor_id,"
                "authorization_ref_id,authorization_version,state,context_policy_version,"
                "conversation_memory_version,user_private_memory_version,task_memory_version,"
                "runtime_version,plan_version,agent_manifest_version,model_revision,prompt_version,"
                "tool_schema_versions,output_schema_id,output_schema_version,tokenizer_revision,"
                "chat_template_hash,max_context,reserved_output,protocol_overhead,safety_margin,"
                "input_tokens,body_artifact_id,pack_hash,input_digest,consumed_at) VALUES "
                "(:id,:task,NULL,NULL,:conversation,:actor,NULL,NULL,'READY','context-policy-v1',"
                "0,0,0,'runtime-v1',NULL,'gateway','model-1','prompt-v1','{}'::jsonb,"
                "NULL,NULL,'tok-1',:digest,8192,1024,512,256,100,:artifact,:digest,:digest,NULL)"
            ),
            {
                "id": ids["pack"],
                "task": ids["task"],
                "conversation": ids["conversation"],
                "actor": ids["actor"],
                "artifact": ids["artifact"],
                "digest": DIGEST,
            },
        )


async def run_canary() -> dict[str, object]:
    database_url = _required_env("KNOWLEDGE_DATABASE_URL")
    identity_key_file = Path(_required_env("KNOWLEDGE_IDENTITY_HMAC_KEY_FILE"))
    workspace_id = _required_env("KNOWLEDGE_WORKSPACE_ID")

    openbao_base_url = _required_env("KNOWLEDGE_OPENBAO_BASE_URL")
    openbao_ca_file = Path(_required_env("KNOWLEDGE_OPENBAO_CA_FILE"))
    openbao_cert_file = Path(_required_env("KNOWLEDGE_OPENBAO_CLIENT_CERT_FILE"))
    openbao_key_file = Path(_required_env("KNOWLEDGE_OPENBAO_CLIENT_KEY_FILE"))
    openbao_token_file = Path(_required_env("KNOWLEDGE_OPENBAO_TOKEN_FILE"))

    bifrost_base_url = _required_env("KNOWLEDGE_BIFROST_BASE_URL")
    bifrost_ca_file = Path(_required_env("KNOWLEDGE_BIFROST_CA_FILE"))
    bifrost_cert_file = Path(_required_env("KNOWLEDGE_BIFROST_CLIENT_CERT_FILE"))
    bifrost_key_file = Path(_required_env("KNOWLEDGE_BIFROST_CLIENT_KEY_FILE"))

    engine = build_async_engine(DatabaseEngineSettings(url=database_url))
    event.listen(engine.sync_engine, "connect", _disable_fk)
    session_factory = build_session_factory(engine)

    ids: dict[str, UUID] = {
        name: uuid4() for name in ("task", "pack", "actor", "conversation", "artifact")
    }
    await _seed(engine, ids)

    openbao = OpenBaoKv2Client(
        base_url=openbao_base_url,
        mount="knowledge-model",
        token_provider=OpenBaoAgentFileTokenProvider(openbao_token_file),
        verify=str(openbao_ca_file),
        cert=(str(openbao_cert_file), str(openbao_key_file)),
    )
    version_resolver = OpenBaoMetadataVersionResolver(openbao)

    bifrost = BifrostHttpClient(
        base_url=bifrost_base_url,
        verify=str(bifrost_ca_file),
        cert=(str(bifrost_cert_file), str(bifrost_key_file)),
    )

    wiring = compose_model_gateway_service(
        session_factory=session_factory,
        permit_authority=PostgresEgressPermitAuthority(session_factory),
        version_resolver=version_resolver,
        bifrost=bifrost,
        identity_hmac_key=_read_identity_key(identity_key_file),
        workspace_id=workspace_id,
    )

    try:
        async with wiring.transaction:
            result = await wiring.service.complete(
                build_plan(ids["pack"], ids["task"], ids["actor"])
            )
    finally:
        await engine.dispose()

    return {
        "model_call_id": str(result.model_call_id),
        "status": result.status.value,
        "text": result.text,
        "usage": result.usage.model_dump(),
        "error_code": result.error_code,
    }


def main() -> int:
    result = asyncio.run(run_canary())
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
