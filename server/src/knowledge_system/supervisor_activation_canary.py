"""DEV-05 exit item: run the real Goal→Plan vertical slice through the full chain.

Wires the REAL adapters — PostgreSQL, OpenBao metadata (mTLS + single-use token), and the
Bifrost HTTP data plane (mTLS) — into `GatewayModel → PydanticSupervisorModel →
SupervisorActivationService → SqlAlchemyGoalPlanWriter`. Seeds a minimal foundation
(task / input / GOAL+PLAN attempts / agent / context pack) so the activation CAS has real
rows to lock, then runs one real Goal call + one real Plan call and atomically activates
Plan v1. Two paid calls total (plus any structural-repair retries the model triggers).

This is a one-shot development validation, not the production runtime. It connects as the
`knowledge` database superuser and disables foreign-key enforcement for the frozen
`task`/`context_pack`/`budget.account` references, mirroring `model_gateway_canary.py`.
The plan carries no Secret, endpoint URL or Provider Key; those are derived inside the
issuer/broker exactly as in the running gateway chain.
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
from knowledge_system.infrastructure.persistence.supervisor_writer import (
    SqlAlchemyGoalPlanWriter,
)
from knowledge_system.infrastructure.supervisor_model import PydanticSupervisorModel
from knowledge_system.modules.model_gateway.domain.budget_quantity import BudgetQuantityV1
from knowledge_system.modules.model_gateway.domain.token_accounting import (
    EstimatedDevTokenAccountingV1,
)
from knowledge_system.modules.tasking.application.supervisor_activation import (
    GoalActivationMaterials,
    PlanActivationMaterials,
    SupervisorActivationService,
)
from knowledge_system.modules.tasking.domain.core_types import (
    ArtifactRefV1,
    PolicySnapshotRefV1,
)
from knowledge_system.modules.tasking.domain.goal_contracts import GoalUnderstandingContextV1
from knowledge_system.modules.tasking.domain.input_contracts import (
    KnowledgeScopeModeV1,
    KnowledgeScopeV1,
    NetworkControlV1,
    NetworkPolicySourceV1,
    OutputContractV1,
    StructuredTaskControlsV1,
)
from knowledge_system.modules.tasking.domain.planning_fragments import (
    CapabilityPlanningViewV1,
    InitialPlanningContextV1,
)

CAPABILITY_ALIAS = "planning_strong"
DIGEST = "a" * 64

# Canary 用最小确定性系统提示：真实模型只拿到冻结 Context（含不透明 query_ref /
# goal_understanding_ref，无查询正文、无上游目标理解正文），故把候选生成的确定性契约钉死
# （可引用资源白名单、local_id 约定），使真实付费调用能确定性地穿过语义校验与 Plan 校验。
# 这不代表生产 Prompt 材料（自然语言模板留待后续登记），只是 Canary 穿链的最小约束。
GOAL_SYSTEM_PROMPT = """\
你是 Supervisor 目标理解模型，依据给定 JSON 上下文生成 GoalUnderstandingModelOutputV1：
1. resource_refs 只能引用 authorized_resource_refs 已列出的资源，绝不虚构；当前为空，必须为 []。
2. blocking_issues 必须为 []。
3. success_criteria_proposals 恰好一条：local_id "sc-1"，evidence_kind "ARTIFACT"，statement 非空。
4. subgoals 与 safe_assumptions 均为 []。
5. primary_goal 与 expected_deliverable 各为一句非空中文。\
"""

PLAN_SYSTEM_PROMPT = """\
你是 Supervisor 初始规划模型，依据给定 JSON 上下文生成 InitialPlanModelOutputV1：
1. items 恰好一条，各字段固定：
   local_id "item-1"；objective 非空中文；executor_requirement "SUPERVISOR"；
   capability_requirements []；input_artifact_refs []；output_schema_ref "report"；
   depends_on_local_ids []；
   iteration_policy {"mode":"SINGLE_PASS","max_iterations":1,"stop_criterion_ids":[]}。
2. success_criteria 恰好一条，各字段固定：
   local_id "crit-1"；statement 非空中文；goal_criterion_local_id "sc-1"；
   target {"kind":"ARTIFACT","producer_item_local_id":"item-1","output_schema_key":"report"}。
3. initial_ready_item_local_ids 固定 ["item-1"]。
4. hypotheses 为 []。\
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
    """Seed the minimal foundation the activation CAS needs to lock real rows."""
    deadline = datetime.now(UTC) + timedelta(hours=1)
    started = datetime.now(UTC) - timedelta(minutes=1)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO iam.organization (id,name,status,version) "
                "VALUES (:id,:name,'ACTIVE',1)"
            ),
            {"id": ids["org"], "name": f"canary-{ids['org']}"},
        )
        username = f"u-{ids['user']}"
        await connection.execute(
            text(
                "INSERT INTO iam.user_account "
                "(id,organization_id,username,normalized_username,display_name,password_hash,"
                "status,must_change_password,failed_login_count,credential_version,version) "
                "VALUES (:id,:org,:username,:username,:username,'hash','ACTIVE',false,0,1,1)"
            ),
            {"id": ids["user"], "org": ids["org"], "username": username},
        )
        await connection.execute(
            text(
                "INSERT INTO content.artifact "
                "(artifact_id,organization_id,owner_id,purpose,artifact_type,storage_backend,"
                "object_key,size_bytes,sha256,detected_media_type,schema_id,schema_version,"
                "schema_digest,data_labels_json,encryption_profile,state,legal_hold,row_version,"
                "created_by_actor_id) VALUES "
                "(:id,:org,:owner,'QUERY','TEXT','S3',:object_key,1,:digest,'text/plain',"
                "'query','v1',:digest,'{}'::jsonb,'dev','AVAILABLE',false,1,:owner)"
            ),
            {
                "id": ids["artifact"],
                "org": ids["org"],
                "owner": ids["user"],
                "object_key": f"canary/{ids['artifact']}",
                "digest": DIGEST,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.conversation "
                "(id,owner_id,organization_id,title,mode,conversation_memory_version,"
                "memory_policy) VALUES (:id,:owner,:org,'canary','internal',0,'OFF')"
            ),
            {"id": ids["conversation"], "owner": ids["user"], "org": ids["org"]},
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.agent_instance "
                "(id,task_id,parent_agent_instance_id,manifest_id,manifest_version,kind,"
                "delegated_scope,status,expires_at,created_reason,created_reason_hash,"
                "completed_at) VALUES "
                "(:id,:task,NULL,'supervisor','v1','SUPERVISOR','{}'::jsonb,'ACTIVE',NULL,"
                "'goal understanding',:digest,NULL)"
            ),
            {"id": ids["agent"], "task": ids["task"], "digest": DIGEST},
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.intelligent_task "
                "(id,organization_id,owner_id,conversation_id,query_artifact_id,"
                "initial_input_id,current_input_id,authorization_ref_id,status,"
                "temporal_workflow_id,last_event_sequence,budget,budget_used,deadline_at,version) "
                "VALUES (:id,:org,:owner,:conversation,:artifact,:input,:input,:agent,'PLANNING',"
                ":temporal,0,'{}'::jsonb,'{}'::jsonb,:deadline,1)"
            ),
            {
                "id": ids["task"],
                "org": ids["org"],
                "owner": ids["user"],
                "conversation": ids["conversation"],
                "artifact": ids["artifact"],
                "input": ids["input"],
                "agent": ids["agent"],
                "temporal": f"agent-task-v1-{ids['task']}",
                "deadline": deadline,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.conversation_message "
                "(id,conversation_id,role,message_kind,content_artifact_id,task_id,sequence,"
                "revision,content_hash,state) VALUES "
                "(:id,:conversation,'USER','USER_QUERY',:artifact,:task,1,1,:digest,'ACTIVE')"
            ),
            {
                "id": ids["message"],
                "conversation": ids["conversation"],
                "artifact": ids["artifact"],
                "task": ids["task"],
                "digest": DIGEST,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.task_input_snapshot "
                "(input_id,task_id,input_revision,message_id,message_artifact_id,actor_id,"
                "organization_id,conversation_id,attachment_binding_refs_json,"
                "knowledge_scope_json,network_policy,network_policy_source,output_contract_json,"
                "selected_resource_refs_json,task_constraint_refs_json,policy_snapshot_ref_json,"
                "locale,timezone,contract_version,snapshot_digest) VALUES "
                "(:input,:task,1,:message,:artifact,:owner,:org,:conversation,"
                "'[]'::jsonb,'{}'::jsonb,'DENY','SYSTEM_DEFAULT','{}'::jsonb,"
                "'[]'::jsonb,'[]'::jsonb,'{}'::jsonb,'zh-CN','Asia/Shanghai',"
                "'task_input_snapshot_v1',:digest)"
            ),
            {
                "input": ids["input"],
                "task": ids["task"],
                "message": ids["message"],
                "artifact": ids["artifact"],
                "owner": ids["user"],
                "org": ids["org"],
                "conversation": ids["conversation"],
                "digest": DIGEST,
            },
        )
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
                "0,0,0,'runtime-v1',NULL,'supervisor','model-1','prompt-v1','{}'::jsonb,"
                "NULL,NULL,'tok-1',:digest,8192,1024,512,256,100,:artifact,:digest,:digest,NULL)"
            ),
            {
                "id": ids["pack"],
                "task": ids["task"],
                "conversation": ids["conversation"],
                "actor": ids["user"],
                "artifact": ids["artifact"],
                "digest": DIGEST,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.task_attempt "
                "(id,task_id,input_id,attempt_kind,attempt_no,conversation_id,agent_instance_id,"
                "agent_registration_key,manifest_id,manifest_version,manifest_digest,"
                "context_pack_id,context_pack_hash,grant_snapshot_digest,input_schema_version,"
                "input_digest,pydantic_run_id,temporal_child_workflow_id,lease_generation,status,"
                "external_effect_state,row_version) VALUES "
                "(:id,:task,:input,'GOAL_UNDERSTANDING',1,:conversation,:agent,'supervisor',"
                "'supervisor','v1',:digest,:pack,:digest,:digest,'goal-input-v1',:digest,"
                ":pydantic,:child,0,'COMPLETED','NO_EFFECT',1)"
            ),
            {
                "id": ids["goal_attempt"],
                "task": ids["task"],
                "input": ids["input"],
                "conversation": ids["conversation"],
                "agent": ids["agent"],
                "pack": ids["pack"],
                "digest": DIGEST,
                "pydantic": f"run-{ids['goal_attempt']}",
                "child": (
                    f"agent-attempt-v1-{str(ids['task']).lower()}-"
                    f"{str(ids['goal_attempt']).lower()}"
                ),
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.task_attempt "
                "(id,task_id,input_id,plan_version,attempt_kind,attempt_no,conversation_id,"
                "agent_instance_id,agent_registration_key,manifest_id,manifest_version,"
                "manifest_digest,context_pack_id,context_pack_hash,grant_snapshot_digest,"
                "input_schema_version,input_digest,pydantic_run_id,temporal_child_workflow_id,"
                "lease_generation,status,external_effect_state,row_version,created_at,started_at) "
                "VALUES (:id,:task,:input,1,'PLAN',1,:conversation,:agent,'supervisor',"
                "'supervisor','v1',:digest,:pack,:digest,:digest,'plan-input-v1',:digest,"
                ":pydantic,:child,0,'RUNNING','NO_EFFECT',1,:started,:started)"
            ),
            {
                "id": ids["attempt"],
                "task": ids["task"],
                "input": ids["input"],
                "conversation": ids["conversation"],
                "agent": ids["agent"],
                "pack": ids["pack"],
                "digest": DIGEST,
                "pydantic": f"run-{ids['attempt']}",
                "child": (
                    f"agent-attempt-v1-{str(ids['task']).lower()}-"
                    f"{str(ids['attempt']).lower()}"
                ),
                "started": started,
            },
        )


def _artifact_ref() -> ArtifactRefV1:
    return ArtifactRefV1(
        artifact_id=uuid4(),
        artifact_type="TEXT",
        sha256="a" * 64,
        schema_id="task_query_text",
        schema_version="v1",
    )


def _policy_ref() -> PolicySnapshotRefV1:
    return PolicySnapshotRefV1(
        policy_snapshot_id=uuid4(),
        policy_revision=1,
        policy_digest="b" * 64,
        observed_at=datetime.now(UTC),
    )


def _controls() -> StructuredTaskControlsV1:
    return StructuredTaskControlsV1(
        knowledge_scope=KnowledgeScopeV1(
            mode=KnowledgeScopeModeV1.DEFAULT_AUTHORIZED,
            directory_ids=(),
            knowledge_version_ids=(),
            include_personal=False,
        ),
        network_policy=NetworkControlV1(value="ASK", source=NetworkPolicySourceV1.SYSTEM_DEFAULT),
        output_contract=OutputContractV1(
            format="MARKDOWN",
            delivery="INLINE",
            language="zh-CN",
            schema_ref=None,
            template_or_skill_ref=None,
            citations="REQUIRED",
            max_output_bytes=10000,
        ),
        selected_resource_refs=(),
    )


def _goal_context(task_id: UUID, input_id: UUID) -> GoalUnderstandingContextV1:
    return GoalUnderstandingContextV1(
        contract_version="goal_understanding_context_v1",
        task_id=task_id,
        input_id=input_id,
        input_snapshot_digest="c" * 64,
        query_ref=_artifact_ref(),
        structured_controls=_controls(),
        effective_constraint_refs=(),
        authorized_resource_refs=(),
        classifier_annotation_ref=None,
        policy_snapshot_ref=_policy_ref(),
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_profile="supervisor-goal-understanding-v1",
        schema_version="supervisor_goal_understanding_v1",
    )


def _plan_context(task_id: UUID, input_id: UUID) -> InitialPlanningContextV1:
    return InitialPlanningContextV1(
        contract_version="supervisor_initial_plan_v1",
        task_id=task_id,
        input_id=input_id,
        goal_understanding_ref=_artifact_ref(),
        effective_constraint_refs=(),
        output_contract_ref=_artifact_ref(),
        resource_refs=(),
        context_pack_ref=_artifact_ref(),
        capability_planning_view_ref=_artifact_ref(),
        budget_reservation_ref=_artifact_ref(),
        policy_snapshot_ref=_policy_ref(),
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_profile="supervisor-initial-plan-v1",
    )


def _planning_view() -> CapabilityPlanningViewV1:
    return CapabilityPlanningViewV1(
        contract_version="capability_planning_view_v1",
        catalog_version="1",
        catalog_digest="d" * 64,
        descriptors=(),
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
        name: uuid4()
        for name in (
            "org",
            "user",
            "artifact",
            "conversation",
            "task",
            "message",
            "input",
            "goal_attempt",
            "attempt",
            "agent",
            "pack",
        )
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

    gateway = GatewayModel(
        wiring.service,
        capability_alias=CAPABILITY_ALIAS,
        task_id=ids["task"],
        account_id=ids["user"],
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
        reserved=BudgetQuantityV1(
            model_call_count=1, input_tokens=4096, output_tokens=4096
        ),
        prompt_version="v1",
        context_policy_version="v1",
        runtime_version="v1",
        agent_manifest_version="v1",
        authorization_version=1,
        execution_generation=1,
        cancellation_generation=0,
        expires_in_seconds=300,
    )

    supervisor = PydanticSupervisorModel(
        gateway,
        goal_system_prompt=GOAL_SYSTEM_PROMPT,
        plan_system_prompt=PLAN_SYSTEM_PROMPT,
    )
    writer = SqlAlchemyGoalPlanWriter(session_factory)
    service = SupervisorActivationService(supervisor, writer)

    now = datetime.now(UTC)
    goal_materials = GoalActivationMaterials(
        task_attempt_id=ids["goal_attempt"],
        expected_task_version=1,
        goal_context_artifact_id=ids["artifact"],
        raw_model_artifact_id=ids["artifact"],
        goal_artifact_id=ids["artifact"],
        gate_artifact_id=ids["artifact"],
        now=now,
    )
    plan_materials = PlanActivationMaterials(
        task_attempt_id=ids["attempt"],
        expected_attempt_row_version=1,
        created_by_agent_instance_id=ids["agent"],
        planning_context_artifact_id=ids["artifact"],
        capability_planning_view_artifact_id=ids["artifact"],
        raw_model_artifact_id=ids["artifact"],
        validated_draft_artifact_id=ids["artifact"],
        validation_artifact_id=ids["artifact"],
        compiled_plan_artifact_id=ids["artifact"],
        planning_view=_planning_view(),
        budget_sufficient=True,
        now=now,
    )

    try:
        async with wiring.transaction:
            result = await service.activate(
                _goal_context(ids["task"], ids["input"]),
                goal_materials,
                _plan_context(ids["task"], ids["input"]),
                plan_materials,
            )
    finally:
        await engine.dispose()

    return {
        "task_id": str(ids["task"]),
        "outcome": result.outcome,
        "understanding_id": str(result.understanding_id),
        "plan_version": result.plan_version,
        "compiled_plan_digest": result.compiled_plan_digest,
    }


def main() -> int:
    result = asyncio.run(run_canary())
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
