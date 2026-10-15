"""真实PostgreSQL上 Goal/Plan 端到端激活的垂直切片验收。

以 `SqlAlchemyGoalPlanWriter` + `FakeSupervisorModel` 驱动 `SupervisorActivationService`：
真实候选装配、Gate、编译后经 CAS 落库。验证任务从 PLANNING → RUNNING，Goal 记录、Plan v1
及其定义子表/运行投影/事件/Outbox 原子就位。
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.supervisor_writer import (
    SqlAlchemyGoalPlanWriter,
)
from knowledge_system.modules.tasking.application.supervisor_activation import (
    GoalActivationMaterials,
    PlanActivationMaterials,
    SupervisorActivationService,
)
from knowledge_system.modules.tasking.domain.core_types import (
    ArtifactRefV1,
    ConstraintRefV1,
    PolicySnapshotRefV1,
)
from knowledge_system.modules.tasking.domain.goal_contracts import (
    GoalBlockingIssueV1,
    GoalBlockingReasonV1,
    GoalCriterionProposalV1,
    GoalUnderstandingContextV1,
    SafeAssumptionReasonV1,
    SafeAssumptionV1,
    SubgoalV1,
)
from knowledge_system.modules.tasking.domain.input_contracts import (
    KnowledgeScopeModeV1,
    KnowledgeScopeV1,
    NetworkControlV1,
    NetworkPolicySourceV1,
    OutputContractV1,
    StructuredTaskControlsV1,
)
from knowledge_system.modules.tasking.domain.planning_fragments import (
    ArtifactCriterionTargetV1,
    CapabilityPlanningViewV1,
    CriterionDraftV1,
    HypothesisDraftV1,
    InitialPlanningContextV1,
    IterationPolicyV1,
    PlanItemDraftV1,
)
from knowledge_system.modules.tasking.domain.supervisor_candidates import (
    GoalUnderstandingModelOutputV1,
    InitialPlanModelOutputV1,
)

TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")
DIGEST = "a" * 64
PLAN_STARTED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def new_ids() -> dict[str, UUID]:
    return {
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


def child_workflow_id(task_id: UUID, attempt_id: UUID) -> str:
    return f"agent-attempt-v1-{str(task_id).lower()}-{str(attempt_id).lower()}"


async def seed_foundation(engine: AsyncEngine, ids: dict[str, UUID]) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO iam.organization "
                "(id,name,status,version) VALUES (:id,:name,'ACTIVE',1)"
            ),
            {"id": ids["org"], "name": f"test-{ids['org']}"},
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
                "object_key": f"test/{ids['artifact']}",
                "digest": DIGEST,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow.conversation "
                "(id,owner_id,organization_id,title,mode,conversation_memory_version,"
                "memory_policy) VALUES (:id,:owner,:org,'test','internal',0,'OFF')"
            ),
            {"id": ids["conversation"], "owner": ids["user"], "org": ids["org"]},
        )


async def seed_task_and_input(engine: AsyncEngine, ids: dict[str, UUID]) -> None:
    deadline = datetime.now(UTC) + timedelta(hours=1)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO workflow.intelligent_task "
                "(id,organization_id,owner_id,conversation_id,query_artifact_id,"
                "initial_input_id,current_input_id,authorization_ref_id,status,"
                "temporal_workflow_id,last_event_sequence,budget,budget_used,deadline_at,version) "
                "VALUES (:id,:org,:owner,:conversation,:artifact,:input,:input,:auth,'PLANNING',"
                ":temporal,0,'{}'::jsonb,'{}'::jsonb,:deadline,1)"
            ),
            {
                "id": ids["task"],
                "org": ids["org"],
                "owner": ids["user"],
                "conversation": ids["conversation"],
                "artifact": ids["artifact"],
                "input": ids["input"],
                "auth": ids["agent"],
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


async def seed_attempt_dependencies(engine: AsyncEngine, ids: dict[str, UUID]) -> None:
    """AgentInstance、Context Pack、GOAL Attempt（COMPLETED）与 PLAN Attempt（RUNNING）。"""

    async with engine.begin() as connection:
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
                "child": child_workflow_id(ids["task"], ids["goal_attempt"]),
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
                "child": child_workflow_id(ids["task"], ids["attempt"]),
                "started": PLAN_STARTED_AT,
            },
        )


def make_artifact_ref() -> ArtifactRefV1:
    return ArtifactRefV1(
        artifact_id=uuid4(),
        artifact_type="TEXT",
        sha256="a" * 64,
        schema_id="task_query_text",
        schema_version="v1",
    )


def make_constraint_ref() -> ConstraintRefV1:
    return ConstraintRefV1(constraint_id=uuid4(), input_id=uuid4(), parser_version="v1")


def make_policy_snapshot_ref() -> PolicySnapshotRefV1:
    return PolicySnapshotRefV1(
        policy_snapshot_id=uuid4(),
        policy_revision=1,
        policy_digest="b" * 64,
        observed_at=datetime.now(UTC),
    )


def make_structured_controls() -> StructuredTaskControlsV1:
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


def make_goal_context(task_id: UUID, input_id: UUID) -> GoalUnderstandingContextV1:
    return GoalUnderstandingContextV1(
        contract_version="goal_understanding_context_v1",
        task_id=task_id,
        input_id=input_id,
        input_snapshot_digest="c" * 64,
        query_ref=make_artifact_ref(),
        structured_controls=make_structured_controls(),
        effective_constraint_refs=(),
        authorized_resource_refs=(),
        classifier_annotation_ref=None,
        policy_snapshot_ref=make_policy_snapshot_ref(),
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_profile="supervisor-goal-understanding-v1",
        schema_version="supervisor_goal_understanding_v1",
    )


def make_plan_context(task_id: UUID, input_id: UUID) -> InitialPlanningContextV1:
    return InitialPlanningContextV1(
        contract_version="supervisor_initial_plan_v1",
        task_id=task_id,
        input_id=input_id,
        goal_understanding_ref=make_artifact_ref(),
        effective_constraint_refs=(),
        output_contract_ref=make_artifact_ref(),
        resource_refs=(),
        context_pack_ref=make_artifact_ref(),
        capability_planning_view_ref=make_artifact_ref(),
        budget_reservation_ref=make_artifact_ref(),
        policy_snapshot_ref=make_policy_snapshot_ref(),
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_profile="supervisor-initial-plan-v1",
    )


def make_goal_output() -> GoalUnderstandingModelOutputV1:
    return GoalUnderstandingModelOutputV1(
        contract_version="supervisor_goal_understanding_model_output_v1",
        primary_goal="完成本周工作目标梳理",
        expected_deliverable="一份Markdown报告",
        subgoals=(SubgoalV1(local_id="subgoal-1", statement="收集资料"),),
        resource_refs=(),
        success_criteria_proposals=(
            GoalCriterionProposalV1(
                local_id="sc-1", statement="交付物存在", evidence_kind="ARTIFACT"
            ),
        ),
        safe_assumptions=(
            SafeAssumptionV1(
                local_id="sa-1",
                statement="默认使用已授权资源",
                reason=SafeAssumptionReasonV1.REVERSIBLE_DEFAULT,
            ),
        ),
        blocking_issues=(),
    )


def make_blocking_goal_output() -> GoalUnderstandingModelOutputV1:
    return GoalUnderstandingModelOutputV1(
        contract_version="supervisor_goal_understanding_model_output_v1",
        primary_goal="完成本周工作目标梳理",
        expected_deliverable="一份Markdown报告",
        subgoals=(),
        resource_refs=(),
        success_criteria_proposals=(
            GoalCriterionProposalV1(
                local_id="sc-1", statement="交付物存在", evidence_kind="ARTIFACT"
            ),
        ),
        safe_assumptions=(),
        blocking_issues=(
            GoalBlockingIssueV1(
                local_id="bi-1",
                reason=GoalBlockingReasonV1.GOAL_AMBIGUOUS,
                field_path="/primary_goal",
                description="目标歧义",
                proposed_question_kind="SHORT_TEXT",
                candidate_resource_refs=(),
            ),
        ),
    )


def make_plan_output() -> InitialPlanModelOutputV1:
    return InitialPlanModelOutputV1(
        contract_version="supervisor_initial_plan_model_output_v1",
        hypotheses=(HypothesisDraftV1(local_id="h-1", statement="假设资料充分"),),
        items=(
            PlanItemDraftV1(
                local_id="item-1",
                objective="生成报告",
                executor_requirement="SUPERVISOR",
                capability_requirements=(),
                input_artifact_refs=(),
                output_schema_ref="report",
                depends_on_local_ids=(),
                iteration_policy=IterationPolicyV1(
                    mode="SINGLE_PASS", max_iterations=1, stop_criterion_ids=()
                ),
            ),
        ),
        success_criteria=(
            CriterionDraftV1(
                local_id="crit-1",
                statement="报告已生成",
                goal_criterion_local_id="sc-1",
                target=ArtifactCriterionTargetV1(
                    kind="ARTIFACT", producer_item_local_id="item-1", output_schema_key="report"
                ),
            ),
        ),
        initial_ready_item_local_ids=("item-1",),
    )


def make_planning_view() -> CapabilityPlanningViewV1:
    return CapabilityPlanningViewV1(
        contract_version="capability_planning_view_v1",
        catalog_version="1",
        catalog_digest="d" * 64,
        descriptors=(),
    )


class FakeSupervisorModel:
    def __init__(self) -> None:
        self._goal_output = make_goal_output()
        self._plan_output = make_plan_output()

    async def generate_goal_understanding(
        self, context: GoalUnderstandingContextV1
    ) -> GoalUnderstandingModelOutputV1:
        return self._goal_output

    async def generate_initial_plan(
        self, context: InitialPlanningContextV1
    ) -> InitialPlanModelOutputV1:
        return self._plan_output


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class PostgresActivationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.engine = build_async_engine(DatabaseEngineSettings(TEST_DATABASE_URL))
        self.session_factory = build_session_factory(self.engine)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def seed_all(self, ids: dict[str, UUID]) -> None:
        await seed_foundation(self.engine, ids)
        await seed_task_and_input(self.engine, ids)
        await seed_attempt_dependencies(self.engine, ids)

    async def count_task(
        self, ids: dict[str, UUID], table: str, where: str = "task_id=:task"
    ) -> int:
        async with self.engine.connect() as connection:
            value = await connection.scalar(
                text(f"SELECT count(*) FROM {table} WHERE {where}"),
                {"task": ids["task"]},
            )
        assert isinstance(value, int)
        return value

    def goal_materials(self, ids: dict[str, UUID]) -> GoalActivationMaterials:
        return GoalActivationMaterials(
            task_attempt_id=ids["goal_attempt"],
            expected_task_version=1,
            goal_context_artifact_id=ids["artifact"],
            raw_model_artifact_id=ids["artifact"],
            goal_artifact_id=ids["artifact"],
            gate_artifact_id=ids["artifact"],
            now=datetime.now(UTC),
        )

    def plan_materials(self, ids: dict[str, UUID]) -> PlanActivationMaterials:
        return PlanActivationMaterials(
            task_attempt_id=ids["attempt"],
            expected_attempt_row_version=1,
            created_by_agent_instance_id=ids["agent"],
            planning_context_artifact_id=ids["artifact"],
            capability_planning_view_artifact_id=ids["artifact"],
            raw_model_artifact_id=ids["artifact"],
            validated_draft_artifact_id=ids["artifact"],
            validation_artifact_id=ids["artifact"],
            compiled_plan_artifact_id=ids["artifact"],
            planning_view=make_planning_view(),
            budget_sufficient=True,
            now=datetime.now(UTC),
        )

    async def test_full_goal_and_plan_activation(self) -> None:
        ids = new_ids()
        await self.seed_all(ids)

        writer = SqlAlchemyGoalPlanWriter(self.session_factory)
        service = SupervisorActivationService(FakeSupervisorModel(), writer)
        result = await service.activate(
            make_goal_context(ids["task"], ids["input"]),
            self.goal_materials(ids),
            make_plan_context(ids["task"], ids["input"]),
            self.plan_materials(ids),
        )

        self.assertEqual(result.outcome, "PLAN_ACTIVATED")
        self.assertEqual(result.plan_version, 1)
        assert result.compiled_plan_digest is not None
        self.assertEqual(len(result.compiled_plan_digest), 64)

        self.assertEqual(await self.count_task(ids, "workflow.task_goal_understanding"), 1)
        self.assertEqual(await self.count_task(ids, "workflow.task_plan_version"), 1)
        self.assertEqual(await self.count_task(ids, "workflow.task_plan_item_definition"), 1)
        self.assertEqual(await self.count_task(ids, "workflow.task_plan_hypothesis"), 1)
        self.assertEqual(await self.count_task(ids, "workflow.task_compiled_criterion"), 1)
        self.assertEqual(await self.count_task(ids, "workflow.plan_item_runtime"), 1)
        # Goal 完成 1 条 + Plan 激活 2 条（PLAN_ACTIVATED + PLAN_ITEM_READY）。
        self.assertEqual(await self.count_task(ids, "workflow.task_event"), 3)
        self.assertEqual(
            await self.count_task(ids, "integration.outbox_message", where="aggregate_id=:task"),
            2,
        )

        async with self.engine.connect() as connection:
            task_row = (
                await connection.execute(
                    text(
                        "SELECT status,active_plan_version,version,last_event_sequence "
                        "FROM workflow.intelligent_task WHERE id=:task"
                    ),
                    {"task": ids["task"]},
                )
            ).one()
            attempt_row = (
                await connection.execute(
                    text(
                        "SELECT status,result_kind,result_schema_version,result_digest,row_version "
                        "FROM workflow.task_attempt WHERE id=:attempt"
                    ),
                    {"attempt": ids["attempt"]},
                )
            ).one()
        # Goal 提交推进 version 1→2；Plan 激活只改状态与 active_plan_version，不再推进 version。
        self.assertEqual(tuple(task_row), ("RUNNING", 1, 2, 3))
        self.assertEqual(
            tuple(attempt_row),
            ("COMPLETED", "SUCCEEDED", "attempt_completed_v1", result.compiled_plan_digest, 2),
        )

    async def test_waiting_user_input_halts_before_plan(self) -> None:
        ids = new_ids()
        await self.seed_all(ids)

        model = FakeSupervisorModel()
        model._goal_output = make_blocking_goal_output()
        writer = SqlAlchemyGoalPlanWriter(self.session_factory)
        service = SupervisorActivationService(model, writer)
        result = await service.activate(
            make_goal_context(ids["task"], ids["input"]),
            self.goal_materials(ids),
            make_plan_context(ids["task"], ids["input"]),
            self.plan_materials(ids),
        )

        self.assertEqual(result.outcome, "WAITING_USER_INPUT")
        self.assertEqual(await self.count_task(ids, "workflow.task_goal_understanding"), 1)
        self.assertEqual(await self.count_task(ids, "workflow.user_input_request"), 1)
        self.assertEqual(await self.count_task(ids, "workflow.task_plan_version"), 0)
        async with self.engine.connect() as connection:
            status = await connection.scalar(
                text("SELECT status FROM workflow.intelligent_task WHERE id=:task"),
                {"task": ids["task"]},
            )
        self.assertEqual(status, "WAITING_USER_INPUT")


if __name__ == "__main__":
    unittest.main()
