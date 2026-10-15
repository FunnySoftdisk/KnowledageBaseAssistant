"""DEV-06 exit item: run one real paid Final-synthesis call through the full chain.

Wires the REAL adapters — PostgreSQL, OpenBao metadata (mTLS + single-use token), and the
Bifrost HTTP data plane (mTLS) — into `GatewayModel → PydanticFinalModel →
FinalSynthesisService → SqlAlchemyTaskResultWriter`. Runs one real Final-synthesis call
(plus any structural-repair retries the model triggers) and commits the content-addressed
`workflow.task_result` v1 row.

This is a one-shot development validation, not the production runtime. It connects as the
`knowledge` database superuser and disables foreign-key enforcement for the frozen
`intelligent_task`/`content.artifact` references, mirroring `model_gateway_canary.py`. It
seeds only a READY `workflow.context_pack` header (id/`pack_hash`/`input_digest` lining up
with `GatewayModel`'s context-pack triple) so the consumption gate passes; no other foundation
rows are seeded because `FinalSynthesisService` performs no CAS against task/attempt — the
task/account/context-pack UUIDs are otherwise opaque and only carried into the egress-permit
and ledger rows (both FK-disabled). The plan carries no Secret, endpoint URL or Provider Key;
those are derived inside the issuer/broker exactly as in the running gateway chain.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from knowledge_system.infrastructure.final_model import PydanticFinalModel
from knowledge_system.infrastructure.model_gateway.bifrost import BifrostHttpClient
from knowledge_system.infrastructure.model_gateway.composition import (
    compose_model_gateway_service,
)
from knowledge_system.infrastructure.model_gateway.egress_permit_issuer import (
    OpenBaoMetadataVersionResolver,
)
from knowledge_system.infrastructure.model_gateway.gateway_model import GatewayModel
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
from knowledge_system.infrastructure.persistence.task_result_writer import (
    SqlAlchemyTaskResultWriter,
)
from knowledge_system.modules.model_gateway.domain.budget_quantity import BudgetQuantityV1
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    EstimatedDevTokenAccountingV1,
)
from knowledge_system.modules.tasking.application.final_synthesis import (
    FinalSynthesisMaterials,
    FinalSynthesisService,
)
from knowledge_system.modules.tasking.domain.compiled_criterion import (
    EVALUATOR_PROFILE_EVIDENCE_GROUNDED,
    CompiledCriterionV1,
    EvidenceSupportPredicateV1,
)
from knowledge_system.modules.tasking.domain.core_types import (
    PolicySnapshotRefV1,
    SchemaRefV1,
    VersionedRefV1,
)
from knowledge_system.modules.tasking.domain.final_admission import FinalSupportCatalogEntryV1
from knowledge_system.modules.tasking.domain.final_candidate import (
    FinalClaimDraftV1,
    FinalParagraphBlockV1,
    FinalSectionDraftV1,
    FinalSynthesisModelOutputV1,
    SemanticCriterionAssessmentV1,
)
from knowledge_system.modules.tasking.domain.final_citation import CitationSourceV1
from knowledge_system.modules.tasking.domain.final_evaluation import CriterionEvaluationStateV1
from knowledge_system.modules.tasking.domain.final_synthesis import (
    FinalProtectedBudgetV1,
    FinalSynthesisSnapshotV1,
    freeze_final_synthesis_snapshot,
)
from knowledge_system.modules.tasking.domain.input_contracts import OutputContractV1
from knowledge_system.modules.tasking.domain.planning_fragments import CriterionSourceKindV1

CAPABILITY_ALIAS = "synthesis_strong"
DIGEST = "a" * 64

# 唯一可引用的已验证事实。真实模型必须把它的 canonical_text 逐字复制进 Claim text 才能穿过
# Claim 支持校验（`CANONICAL_TEXT_MISMATCH` 为可修复路径）；目录只有这一条 `VERIFIED_CLAIM`，
# 其 evidence_ref 绑定唯一的 Citation Source。这是 Canary 穿链的最小确定性事实，不代表生产素材。
CANONICAL_TEXT = "关键事实：模型网关出网调用必须经过一次性许可校验，且不读取任何长期密钥。"

# Canary 用最小确定性系统提示：真实模型只拿到冻结的 snapshot 与 support_catalog，故把候选生成的
# 确定性契约（可引用目录白名单、local_id 约定、逐字复制 canonical_text、逐字复制 criterion_id）
# 钉死，使真实付费调用能确定性地穿过 Claim 支持校验、语义 Criterion 求值与 Citation 解析。
FINAL_SYSTEM_PROMPT = """\
你是 Final 综合候选生成模型。输入是 JSON：{"snapshot": ..., "support_catalog": [...]}。
依据输入生成 FinalSynthesisModelOutputV1（严格 Schema，contract_version 固定为
"final_synthesis_model_output_v1"），并满足以下确定性契约：
1. title：一句非空中文。
2. claims：恰好一条，各字段固定：
   local_claim_id "c1"；claim_kind "SOURCE_FACT"；
   text 必须逐字等于 support_catalog 中 catalog_id=="vc1" 的 canonical_text
   （原样复制，不得改写/翻译/增删）；
   support_catalog_ids ["vc1"]；criterion_ids []；conflict_set_ids []。
3. sections：恰好一节，各字段固定：
   local_section_id "s1"；heading 非空中文；
   blocks 恰好一个段落块：{"block_kind":"PARAGRAPH","claim_local_ids":["c1"]}。
4. semantic_criterion_assessments：恰好一条，各字段固定：
   criterion_id 逐字等于 snapshot.compiled_criteria[0].criterion_id；
   assessment "SUPPORTED"；supporting_catalog_ids ["vc1"]；claim_local_ids ["c1"]；note 非空。
5. gap_reports []；selected_result_artifact_catalog_ids []。\
"""


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


def _disable_fk(dbapi_connection: object, connection_record: object) -> None:
    del connection_record
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("SET session_replication_role = replica")
    cursor.close()


async def _seed(engine: AsyncEngine, ids: dict[str, UUID]) -> None:
    """Seed a READY `workflow.context_pack` header so the gateway consumption gate passes.

    `FinalSynthesisService` performs no CAS against task/attempt, so the task/actor/conversation/
    artifact UUIDs are opaque (FK enforcement is disabled); only the pack header identity —
    `id`/`pack_hash`/`input_digest` — must line up with `GatewayModel`'s context-pack triple.
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
                "0,0,0,'runtime-v1',NULL,'final-synthesis','model-1','prompt-v1','{}'::jsonb,"
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


def _evid(i: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{i:012d}")


def _vref(ref_id: UUID) -> VersionedRefV1:
    return VersionedRefV1(ref_id=ref_id, version=1, integrity_digest=DIGEST)


def _criterion() -> CompiledCriterionV1:
    return CompiledCriterionV1(
        criterion_id=uuid4(),
        statement="关键事实有据可查",
        source_kind=CriterionSourceKindV1.GOAL,
        source_refs=("gc1",),
        requirement_level="REQUIRED",
        completion_role="CORE_DELIVERABLE",
        protected=True,
        evaluation_mode="EVIDENCE_GROUNDED_SEMANTIC",
        predicate=EvidenceSupportPredicateV1(
            kind="EVIDENCE_SUPPORT", producer_plan_item_id=uuid4(), min_citations=1
        ),
        applicability=None,
        evaluator_profile=EVALUATOR_PROFILE_EVIDENCE_GROUNDED,
        digest=DIGEST,
    )


def _contract() -> OutputContractV1:
    return OutputContractV1(
        format="MARKDOWN",
        delivery="INLINE",
        language="zh-CN",
        schema_ref=None,
        template_or_skill_ref=None,
        citations="OPTIONAL",
        max_output_bytes=100_000,
    )


def make_snapshot(task_id: UUID) -> FinalSynthesisSnapshotV1:
    return freeze_final_synthesis_snapshot(
        FinalSynthesisSnapshotV1(
            contract_version="final_synthesis_snapshot_v1",
            task_id=task_id,
            runtime_decision_id=uuid4(),
            input_id=uuid4(),
            plan_version=1,
            work_version=1,
            execution_generation=0,
            cancellation_generation=0,
            event_high_watermark=0,
            compiled_criteria=(_criterion(),),
            effective_constraint_refs=(),
            output_contract=_contract(),
            adopted_result_refs=(),
            gap_summaries=(),
            conflict_ids=(),
            unresolved_subgoal_ids=(),
            permanent_blocker_ids=(),
            citation_candidate_catalog_ids=(),
            result_artifact_candidate_catalog_ids=(),
            policy_snapshot_ref=PolicySnapshotRefV1(
                policy_snapshot_id=uuid4(),
                policy_revision=1,
                policy_digest=DIGEST,
                observed_at=datetime.now(UTC),
            ),
            source_visibility=(),
            authorization_revoked=False,
            degraded=False,
            final_protected_budget=FinalProtectedBudgetV1(
                model_requests=2, input_tokens=0, output_tokens=0
            ),
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
            builder_profile="final-synthesis-context-builder-v1",
            builder_schema_ref=SchemaRefV1(
                schema_id="final-synthesis", schema_version="v1", schema_digest=DIGEST
            ),
            snapshot_digest="0" * 64,
        )
    )


def make_catalog() -> tuple[FinalSupportCatalogEntryV1, ...]:
    return (
        FinalSupportCatalogEntryV1(
            catalog_id="vc1",
            kind="VERIFIED_CLAIM",
            source_ref=_vref(_evid(900)),
            canonical_text=CANONICAL_TEXT,
            verdict="SUPPORTED",
            artifact_kind=None,
            evidence_ref=_vref(_evid(1)),
        ),
    )


def make_sources() -> tuple[CitationSourceV1, ...]:
    return (
        CitationSourceV1(
            evidence_ref_id=_evid(1),
            source_domain="EXTERNAL_WEB",
            locator_kind="WEB",
            locator_value="https://example.com",
            preview=None,
        ),
    )


def make_materials(snap: FinalSynthesisSnapshotV1) -> FinalSynthesisMaterials:
    return FinalSynthesisMaterials(
        snapshot=snap,
        catalog=make_catalog(),
        citation_sources=make_sources(),
        evaluation_state=CriterionEvaluationStateV1(
            completed_item_ids=(), adopted_outputs=(), constraint_checks=()
        ),
        degradation_flags=(),
        output_contract_complete=True,
        final_attempt_id=uuid4(),
        candidate_artifact_id=uuid4(),
        validation_artifact_id=uuid4(),
        criterion_evaluation_set_artifact_id=uuid4(),
        completion_gate_decision_artifact_id=uuid4(),
        generated_at=datetime.now(UTC),
    )


def build_canonical_output(snap: FinalSynthesisSnapshotV1) -> FinalSynthesisModelOutputV1:
    """按 `FINAL_SYSTEM_PROMPT` 契约构造的目标候选输出，供离线盲测的 FunctionModel 替身复用。"""

    criterion_id = snap.compiled_criteria[0].criterion_id
    return FinalSynthesisModelOutputV1(
        contract_version="final_synthesis_model_output_v1",
        title="报告标题",
        claims=(
            FinalClaimDraftV1(
                local_claim_id="c1",
                claim_kind="SOURCE_FACT",
                text=CANONICAL_TEXT,
                support_catalog_ids=("vc1",),
                criterion_ids=(),
                conflict_set_ids=(),
            ),
        ),
        sections=(
            FinalSectionDraftV1(
                local_section_id="s1",
                heading="概述",
                blocks=(
                    FinalParagraphBlockV1(
                        block_kind="PARAGRAPH", claim_local_ids=("c1",)
                    ),
                ),
            ),
        ),
        semantic_criterion_assessments=(
            SemanticCriterionAssessmentV1(
                criterion_id=criterion_id,
                assessment="SUPPORTED",
                supporting_catalog_ids=("vc1",),
                claim_local_ids=("c1",),
                note="证据支持",
            ),
        ),
        gap_reports=(),
        selected_result_artifact_catalog_ids=(),
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

    task_id = ids["task"]
    gateway = GatewayModel(
        wiring.service,
        capability_alias=CAPABILITY_ALIAS,
        task_id=task_id,
        account_id=ids["actor"],
        context_pack_id=ids["pack"],
        # 与 _seed 落库的 context_pack 头一致（pack_hash/input_digest 均为 DIGEST），
        # 使消费门真实穿链。
        context_pack_hash=DIGEST,
        context_pack_input_digest=DIGEST,
        token_accounting=EstimatedDevTokenAccountingV1(
            mode="ESTIMATED_DEV",
            estimated=True,
            estimated_input_tokens=1024,
            estimator_revision="est-1",
            serialized_request_bytes=16384,
            protocol_overhead_tokens=8,
        ),
        reserved=BudgetQuantityV1(model_call_count=1, input_tokens=4096, output_tokens=4096),
        prompt_version="v1",
        context_policy_version="v1",
        runtime_version="v1",
        agent_manifest_version="v1",
        authorization_version=1,
        execution_generation=1,
        cancellation_generation=0,
        expires_in_seconds=300,
    )

    final_model = PydanticFinalModel(gateway, system_prompt=FINAL_SYSTEM_PROMPT)
    writer = SqlAlchemyTaskResultWriter(session_factory)
    service = FinalSynthesisService(final_model, writer)

    snap = make_snapshot(task_id)
    materials = make_materials(snap)

    try:
        async with wiring.transaction:
            result = await service.finalize(materials)
    finally:
        await engine.dispose()

    return {
        "task_id": str(task_id),
        "outcome": result.outcome,
        "answer_status": result.answer_status.value if result.answer_status else None,
        "result_version": result.result_version,
        "commit_disposition": result.commit_disposition.value
        if result.commit_disposition
        else None,
        "result_commit_digest": result.result_commit_digest,
        "validation_verdict": result.validation.verdict if result.validation else None,
    }


def main() -> int:
    result = asyncio.run(run_canary())
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
