"""真实PostgreSQL上的Plan激活CAS、原子写集、故障与并发测试。"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    GoalCompletionConflictError,
    GoalCompletionWriteSet,
    PlanActivationConflictError,
    PlanActivationWriteSet,
    SqlAlchemyUnitOfWork,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.attempt_models import TaskAttemptRecord
from knowledge_system.infrastructure.persistence.planning_models import (
    PlanItemRuntimeRecord,
    TaskGoalUnderstandingRecord,
    TaskPlanItemDefinitionRecord,
    TaskPlanVersionRecord,
)
from knowledge_system.infrastructure.persistence.task_models import (
    OutboxMessageRecord,
    TaskEventRecord,
    UserInputRequestRecord,
)

TEST_DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")
DIGEST = "a" * 64


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
            "goal",
            "goal_attempt",
            "attempt",
            "agent",
            "pack",
            "item",
            "event",
            "event2",
            "outbox",
            "outbox2",
            "request",
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
    """落入Goal理解阶段已提交的AgentInstance、Context Pack与GOAL Attempt。"""

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


def make_goal_understanding(ids: dict[str, UUID]) -> TaskGoalUnderstandingRecord:
    return TaskGoalUnderstandingRecord(
        understanding_id=ids["goal"],
        task_id=ids["task"],
        input_id=ids["input"],
        task_attempt_id=ids["goal_attempt"],
        contract_version="supervisor_goal_understanding_v1",
        prompt_profile="supervisor-goal-understanding-v1",
        prompt_profile_digest=DIGEST,
        model_alias="planning_strong",
        goal_context_artifact_id=ids["artifact"],
        context_digest=DIGEST,
        raw_model_artifact_id=ids["artifact"],
        goal_artifact_id=ids["artifact"],
        understanding_digest=DIGEST,
        gate_profile="clarification_gate_v1",
        gate_profile_digest=DIGEST,
        gate_artifact_id=ids["artifact"],
        gate_digest=DIGEST,
        gate_outcome="READY_TO_PLAN",
        subgoal_count=1,
        criterion_count=1,
        assumption_count=0,
        blocking_issue_count=0,
    )


def make_plan_attempt(ids: dict[str, UUID], *, plan_version: int) -> TaskAttemptRecord:
    return TaskAttemptRecord(
        id=ids["attempt"],
        task_id=ids["task"],
        input_id=ids["input"],
        plan_version=plan_version,
        attempt_kind="PLAN",
        attempt_no=1,
        conversation_id=ids["conversation"],
        agent_instance_id=ids["agent"],
        agent_registration_key="supervisor",
        manifest_id="supervisor",
        manifest_version="v1",
        manifest_digest=DIGEST,
        context_pack_id=ids["pack"],
        context_pack_hash=DIGEST,
        grant_snapshot_digest=DIGEST,
        input_schema_version="goal-input-v1",
        input_digest=DIGEST,
        pydantic_run_id=f"run-{ids['attempt']}",
        temporal_child_workflow_id=child_workflow_id(ids["task"], ids["attempt"]),
        status="RUNNING",
        external_effect_state="NO_EFFECT",
    )


def make_goal_attempt(ids: dict[str, UUID], *, attempt_no: int) -> TaskAttemptRecord:
    attempt = make_plan_attempt(ids, plan_version=1)
    attempt.id = ids["goal_attempt"]
    attempt.plan_version = None
    attempt.attempt_kind = "GOAL_UNDERSTANDING"
    attempt.attempt_no = attempt_no
    attempt.pydantic_run_id = f"run-{ids['goal_attempt']}"
    attempt.temporal_child_workflow_id = child_workflow_id(ids["task"], ids["goal_attempt"])
    attempt.status = "COMPLETED"
    return attempt


def make_plan(
    ids: dict[str, UUID], *, plan_version: int, predecessor: int | None
) -> TaskPlanVersionRecord:
    return TaskPlanVersionRecord(
        task_id=ids["task"],
        plan_version=plan_version,
        predecessor_version=predecessor,
        input_id=ids["input"],
        task_attempt_id=ids["attempt"],
        goal_understanding_id=ids["goal"],
        planning_context_artifact_id=ids["artifact"],
        capability_planning_view_artifact_id=ids["artifact"],
        raw_model_artifact_id=ids["artifact"],
        validated_draft_artifact_id=ids["artifact"],
        validation_artifact_id=ids["artifact"],
        compiled_plan_artifact_id=ids["artifact"],
        compiled_plan_digest=DIGEST,
        revision_reason="NEW_TASK",
        status="ACTIVE",
        created_by_agent_instance_id=ids["agent"],
        activated_at=datetime.now(UTC),
        completed_at=None,
    )


def make_item_and_runtime(
    ids: dict[str, UUID], *, plan_version: int
) -> tuple[TaskPlanItemDefinitionRecord, PlanItemRuntimeRecord]:
    item = TaskPlanItemDefinitionRecord(
        task_id=ids["task"],
        plan_version=plan_version,
        plan_item_id=ids["item"],
        source_local_id="item-1",
        objective="deliver the requested answer",
        executor_requirement="SUPERVISOR",
        capability_requirements_json=[],
        candidate_capability_refs_json=[],
        input_artifact_refs_json=[],
        output_schema_ref_json={},
        depends_on_item_ids_json=[],
        iteration_policy_json={},
        initial_state="READY",
        definition_digest=DIGEST,
    )
    runtime = PlanItemRuntimeRecord(
        task_id=ids["task"],
        plan_version=plan_version,
        plan_item_id=ids["item"],
        status="READY",
        current_iteration=0,
        active_task_attempt_id=None,
        last_error_code=None,
        row_version=1,
        started_at=None,
        completed_at=None,
    )
    return item, runtime


def make_event(
    ids: dict[str, UUID], *, event_key: str, payload_digest: str = DIGEST
) -> TaskEventRecord:
    if event_key == "event":
        event_type, business_ref_type, business_ref_id = "PLAN_ACTIVATED", "TASK", ids["task"]
    else:
        event_type, business_ref_type, business_ref_id = "PLAN_ITEM_READY", "PLAN_ITEM", ids["item"]
    return TaskEventRecord(
        id=ids[event_key],
        task_id=ids["task"],
        sequence=0,
        event_type=event_type,
        event_schema_version="task-event-v1",
        workflow_relevant=True,
        business_ref_type=business_ref_type,
        business_ref_id=business_ref_id,
        business_version=1,
        observation_expected_count=0,
        payload_json={},
        payload_digest=payload_digest,
    )


def make_outbox(ids: dict[str, UUID], *, event_key: str) -> OutboxMessageRecord:
    return OutboxMessageRecord(
        id=ids["outbox"],
        event_id=ids[event_key],
        aggregate_type="TASK",
        aggregate_id=ids["task"],
        aggregate_version=1,
        destination="MQ",
        schema_version="outbox-v1",
        payload_json={},
        payload_digest=DIGEST,
        publish_status="PENDING",
        available_at=datetime.now(UTC),
        attempt_count=0,
        row_version=1,
    )


def make_goal_event(
    ids: dict[str, UUID], *, event_key: str, event_type: str, payload_digest: str = DIGEST
) -> TaskEventRecord:
    business_ref_id = (
        ids["goal"] if event_type == "GOAL_UNDERSTANDING_COMPLETED" else ids["request"]
    )
    return TaskEventRecord(
        id=ids[event_key],
        task_id=ids["task"],
        sequence=0,
        event_type=event_type,
        event_schema_version="task-event-v1",
        workflow_relevant=True,
        business_ref_type="GOAL"
        if event_type == "GOAL_UNDERSTANDING_COMPLETED"
        else "USER_INPUT_REQUEST",
        business_ref_id=business_ref_id,
        business_version=1,
        observation_expected_count=0,
        payload_json={},
        payload_digest=payload_digest,
    )


def make_goal_outbox(
    ids: dict[str, UUID], *, event_key: str, outbox_key: str
) -> OutboxMessageRecord:
    message = make_outbox(ids, event_key=event_key)
    message.id = ids[outbox_key]
    return message


def make_goal_completion_write_set(
    ids: dict[str, UUID], *, request_user_input: bool = False
) -> GoalCompletionWriteSet:
    goal = make_goal_understanding(ids)
    completion_event = make_goal_event(
        ids, event_key="event", event_type="GOAL_UNDERSTANDING_COMPLETED"
    )
    events = (completion_event,)
    outbox = (make_goal_outbox(ids, event_key="event", outbox_key="outbox"),)
    request = None
    if request_user_input:
        goal.gate_outcome = "REQUEST_USER_INPUT"
        goal.blocking_issue_count = 1
        request = UserInputRequestRecord(
            id=ids["request"],
            task_id=ids["task"],
            input_id=ids["input"],
            origin="GOAL_CLARIFICATION",
            source_task_attempt_id=ids["goal_attempt"],
            question_artifact_id=ids["artifact"],
            input_schema_json={},
            response_contract_version="clarification_response_contract_v1",
            display_summary="需要补充信息",
            status="PENDING",
            response_artifact_id=None,
            response_size_bytes=None,
            goal_understanding_id=ids["goal"],
            clarification_round_no=1,
            resume_bundle_id=None,
            deferred_call_id=None,
            runtime_blocking_issue_id=None,
            issue_fingerprint=None,
            request_version=1,
            row_version=1,
            requested_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            submitted_at=None,
        )
        request_event = make_goal_event(ids, event_key="event2", event_type="USER_INPUT_REQUESTED")
        events = (completion_event, request_event)
        outbox = (make_goal_outbox(ids, event_key="event2", outbox_key="outbox2"),)
    return GoalCompletionWriteSet(
        goal_understanding=goal,
        expected_task_version=1,
        user_input_request=request,
        events=events,
        outbox_messages=outbox,
    )


def make_plan_write_set(
    ids: dict[str, UUID],
    *,
    plan_version: int,
    predecessor: int | None,
) -> PlanActivationWriteSet:
    item, runtime = make_item_and_runtime(ids, plan_version=plan_version)
    return PlanActivationWriteSet(
        plan_version=make_plan(ids, plan_version=plan_version, predecessor=predecessor),
        task_attempt=make_plan_attempt(ids, plan_version=plan_version),
        goal_understanding=make_goal_understanding(ids),
        items=(item,),
        runtime_rows=(runtime,),
        events=(make_event(ids, event_key="event"), make_event(ids, event_key="event2")),
        outbox_messages=(make_outbox(ids, event_key="event"),),
    )


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class PostgresPlanActivationTests(unittest.IsolatedAsyncioTestCase):
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

    async def activate(self, write_set: PlanActivationWriteSet) -> None:
        async with SqlAlchemyUnitOfWork(self.session_factory) as unit_of_work:
            await unit_of_work.plans.activate_plan_version(write_set)
            await unit_of_work.commit()

    async def complete_goal(self, write_set: GoalCompletionWriteSet) -> None:
        async with SqlAlchemyUnitOfWork(self.session_factory) as unit_of_work:
            await unit_of_work.plans.complete_goal_understanding(write_set)
            await unit_of_work.commit()

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

    async def test_activate_first_plan_version_atomically(self) -> None:
        ids = new_ids()
        await self.seed_all(ids)

        await self.activate(make_plan_write_set(ids, plan_version=1, predecessor=None))

        self.assertEqual(
            await self.count_task(ids, "workflow.task_plan_version"),
            1,
        )
        self.assertEqual(
            await self.count_task(ids, "workflow.task_attempt"),
            2,  # goal attempt + plan attempt
        )
        self.assertEqual(
            await self.count_task(ids, "workflow.task_goal_understanding"),
            1,
        )
        self.assertEqual(
            await self.count_task(ids, "workflow.task_plan_item_definition"),
            1,
        )
        self.assertEqual(
            await self.count_task(ids, "workflow.plan_item_runtime"),
            1,
        )
        self.assertEqual(
            await self.count_task(ids, "workflow.task_event"),
            2,
        )
        self.assertEqual(
            await self.count_task(ids, "integration.outbox_message", where="aggregate_id=:task"),
            1,
        )
        async with self.engine.connect() as connection:
            active_plan = await connection.scalar(
                text("SELECT active_plan_version FROM workflow.intelligent_task WHERE id=:task"),
                {"task": ids["task"]},
            )
            status = await connection.scalar(
                text("SELECT status FROM workflow.intelligent_task WHERE id=:task"),
                {"task": ids["task"]},
            )
            last_sequence = await connection.scalar(
                text("SELECT last_event_sequence FROM workflow.intelligent_task WHERE id=:task"),
                {"task": ids["task"]},
            )
            sequences = (
                (
                    await connection.execute(
                        text(
                            "SELECT sequence FROM workflow.task_event "
                            "WHERE task_id=:task ORDER BY sequence"
                        ),
                        {"task": ids["task"]},
                    )
                )
                .scalars()
                .all()
            )
        self.assertEqual(active_plan, 1)
        self.assertEqual(status, "RUNNING")
        self.assertEqual(last_sequence, 2)
        self.assertEqual(list(sequences), [1, 2])

    async def test_ready_goal_completion_is_atomic_and_idempotent(self) -> None:
        ids = new_ids()
        await self.seed_all(ids)
        write_set = make_goal_completion_write_set(ids)

        await self.complete_goal(write_set)
        await self.complete_goal(make_goal_completion_write_set(ids))

        self.assertEqual(await self.count_task(ids, "workflow.task_goal_understanding"), 1)
        self.assertEqual(await self.count_task(ids, "workflow.task_event"), 1)
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT status,version,last_event_sequence FROM workflow.intelligent_task "
                        "WHERE id=:task"
                    ),
                    {"task": ids["task"]},
                )
            ).one()
        self.assertEqual(tuple(row), ("PLANNING", 2, 1))

    async def test_goal_clarification_creates_request_and_waits(self) -> None:
        ids = new_ids()
        await self.seed_all(ids)

        await self.complete_goal(make_goal_completion_write_set(ids, request_user_input=True))

        self.assertEqual(await self.count_task(ids, "workflow.user_input_request"), 1)
        self.assertEqual(await self.count_task(ids, "workflow.task_event"), 2)
        async with self.engine.connect() as connection:
            status = await connection.scalar(
                text("SELECT status FROM workflow.intelligent_task WHERE id=:task"),
                {"task": ids["task"]},
            )
        self.assertEqual(status, "WAITING_USER_INPUT")

    async def test_goal_fault_rolls_back_request_events_and_task_transition(self) -> None:
        ids = new_ids()
        await self.seed_all(ids)
        write_set = make_goal_completion_write_set(ids, request_user_input=True)
        write_set.events[1].payload_digest = "Z" * 64

        with self.assertRaises(IntegrityError):
            await self.complete_goal(write_set)

        for table in (
            "workflow.task_goal_understanding",
            "workflow.user_input_request",
            "workflow.task_event",
        ):
            self.assertEqual(await self.count_task(ids, table), 0, table)
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT status,version,last_event_sequence FROM workflow.intelligent_task "
                        "WHERE id=:task"
                    ),
                    {"task": ids["task"]},
                )
            ).one()
        self.assertEqual(tuple(row), ("PLANNING", 1, 0))

    async def test_goal_stale_task_version_is_rejected_without_writes(self) -> None:
        ids = new_ids()
        await self.seed_all(ids)
        write_set = make_goal_completion_write_set(ids)
        stale = GoalCompletionWriteSet(
            goal_understanding=write_set.goal_understanding,
            expected_task_version=2,
            events=write_set.events,
            outbox_messages=write_set.outbox_messages,
        )

        with self.assertRaisesRegex(GoalCompletionConflictError, "GOAL_TASK_VERSION_CONFLICT"):
            await self.complete_goal(stale)
        self.assertEqual(await self.count_task(ids, "workflow.task_goal_understanding"), 0)

    async def test_concurrent_goal_completion_has_single_version_winner(self) -> None:
        ids = new_ids()
        await self.seed_all(ids)
        first = make_goal_completion_write_set(ids)
        rival_ids = {
            **ids,
            "goal": uuid4(),
            "goal_attempt": uuid4(),
            "attempt": uuid4(),
            "event": uuid4(),
            "outbox": uuid4(),
        }
        second_base = make_goal_completion_write_set(rival_ids)
        second = GoalCompletionWriteSet(
            goal_understanding=second_base.goal_understanding,
            expected_task_version=1,
            task_attempt=make_goal_attempt(rival_ids, attempt_no=2),
            events=second_base.events,
            outbox_messages=second_base.outbox_messages,
        )

        results = await asyncio.gather(
            self.complete_goal(first), self.complete_goal(second), return_exceptions=True
        )
        successes = [result for result in results if not isinstance(result, BaseException)]
        conflicts = [
            result for result in results if isinstance(result, GoalCompletionConflictError)
        ]
        self.assertEqual(len(successes), 1, results)
        self.assertEqual(len(conflicts), 1, results)
        self.assertEqual(await self.count_task(ids, "workflow.task_goal_understanding"), 1)
        self.assertEqual(await self.count_task(ids, "workflow.task_event"), 1)

    async def test_rollback_fault_injection_leaves_no_orphan_records(self) -> None:
        ids = new_ids()
        await self.seed_all(ids)

        write_set = make_plan_write_set(ids, plan_version=1, predecessor=None)
        bad_event = make_event(ids, event_key="event", payload_digest="Z" * 64)
        faulted = PlanActivationWriteSet(
            plan_version=write_set.plan_version,
            task_attempt=write_set.task_attempt,
            goal_understanding=write_set.goal_understanding,
            items=write_set.items,
            runtime_rows=write_set.runtime_rows,
            events=(bad_event, write_set.events[1]),
            outbox_messages=write_set.outbox_messages,
        )
        with self.assertRaises(IntegrityError):
            await self.activate(faulted)

        async with self.engine.connect() as connection:
            status = await connection.scalar(
                text("SELECT status FROM workflow.intelligent_task WHERE id=:task"),
                {"task": ids["task"]},
            )
            last_sequence = await connection.scalar(
                text("SELECT last_event_sequence FROM workflow.intelligent_task WHERE id=:task"),
                {"task": ids["task"]},
            )
        self.assertEqual(status, "PLANNING")
        self.assertEqual(last_sequence, 0)
        for table, where in (
            ("workflow.task_plan_version", "task_id=:task"),
            ("workflow.task_plan_item_definition", "task_id=:task"),
            ("workflow.plan_item_runtime", "task_id=:task"),
            ("workflow.task_goal_understanding", "task_id=:task"),
            ("workflow.task_event", "task_id=:task"),
            ("integration.outbox_message", "aggregate_id=:task"),
        ):
            self.assertEqual(
                await self.count_task(ids, table, where=where),
                0,
                table,
            )
        # 故障注入不应破坏种子阶段的Goal Attempt与Context Pack。
        self.assertEqual(
            await self.count_task(ids, "workflow.task_attempt"),
            1,  # 仅GOAL Attempt，PLAN Attempt随回滚消失
        )

    async def test_same_attempt_and_digest_replay_is_idempotent(self) -> None:
        ids = new_ids()
        await self.seed_all(ids)

        await self.activate(make_plan_write_set(ids, plan_version=1, predecessor=None))
        await self.activate(make_plan_write_set(ids, plan_version=1, predecessor=None))

        self.assertEqual(
            await self.count_task(ids, "workflow.task_plan_version"),
            1,
        )
        self.assertEqual(
            await self.count_task(ids, "workflow.task_event"),
            2,
        )
        self.assertEqual(
            await self.count_task(ids, "workflow.task_attempt"),
            2,  # 第二次PLAN Attempt未落库
        )

    async def test_same_version_with_different_digest_is_rejected(self) -> None:
        ids = new_ids()
        await self.seed_all(ids)
        await self.activate(make_plan_write_set(ids, plan_version=1, predecessor=None))
        changed = make_plan_write_set(ids, plan_version=1, predecessor=None)
        changed.plan_version.compiled_plan_digest = "b" * 64
        with self.assertRaises(PlanActivationConflictError):
            await self.activate(changed)

    async def test_non_planning_task_is_rejected_before_any_plan_write(self) -> None:
        ids = new_ids()
        await self.seed_all(ids)
        async with self.engine.begin() as connection:
            await connection.execute(
                text("UPDATE workflow.intelligent_task SET status='QUEUED' WHERE id=:task"),
                {"task": ids["task"]},
            )
        with self.assertRaisesRegex(
            PlanActivationConflictError, "PLAN_ACTIVATION_TASK_STATUS_CONFLICT"
        ):
            await self.activate(make_plan_write_set(ids, plan_version=1, predecessor=None))
        self.assertEqual(await self.count_task(ids, "workflow.task_plan_version"), 0)

    async def test_stale_input_and_non_ready_goal_are_rejected(self) -> None:
        stale_ids = new_ids()
        await self.seed_all(stale_ids)
        stale = make_plan_write_set(stale_ids, plan_version=1, predecessor=None)
        other_input = uuid4()
        stale.plan_version.input_id = other_input
        assert stale.task_attempt is not None
        stale.task_attempt.input_id = other_input
        assert stale.goal_understanding is not None
        stale.goal_understanding.input_id = other_input
        with self.assertRaisesRegex(PlanActivationConflictError, "PLAN_ACTIVATION_INPUT_CONFLICT"):
            await self.activate(stale)

        blocked_ids = new_ids()
        await self.seed_all(blocked_ids)
        blocked = make_plan_write_set(blocked_ids, plan_version=1, predecessor=None)
        assert blocked.goal_understanding is not None
        blocked.goal_understanding.gate_outcome = "REQUEST_USER_INPUT"
        blocked.goal_understanding.blocking_issue_count = 1
        with self.assertRaisesRegex(PlanActivationConflictError, "PLAN_ACTIVATION_GOAL_CONFLICT"):
            await self.activate(blocked)

    async def test_expired_deadline_is_rejected_and_rolls_back_write_set(self) -> None:
        ids = new_ids()
        await self.seed_all(ids)
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE workflow.intelligent_task "
                    "SET created_at=now()-interval '2 hours', deadline_at=now()-interval '1 hour' "
                    "WHERE id=:task"
                ),
                {"task": ids["task"]},
            )
        with self.assertRaisesRegex(PlanActivationConflictError, "PLAN_ACTIVATION_CAS_CONFLICT"):
            await self.activate(make_plan_write_set(ids, plan_version=1, predecessor=None))
        self.assertEqual(await self.count_task(ids, "workflow.task_plan_version"), 0)
        self.assertEqual(await self.count_task(ids, "workflow.task_event"), 0)

    async def test_concurrent_activation_yields_single_active_plan(self) -> None:
        ids = new_ids()
        await self.seed_all(ids)

        first = make_plan_write_set(ids, plan_version=1, predecessor=None)
        rival_ids = {
            **ids,
            "attempt": uuid4(),
            "event": uuid4(),
            "event2": uuid4(),
            "outbox": uuid4(),
        }
        second = make_plan_write_set(rival_ids, plan_version=1, predecessor=None)
        results = await asyncio.gather(
            self.activate(first),
            self.activate(second),
            return_exceptions=True,
        )
        successes = [r for r in results if not isinstance(r, BaseException)]
        conflicts = [r for r in results if isinstance(r, PlanActivationConflictError)]
        self.assertEqual(len(successes), 1, results)
        self.assertEqual(len(conflicts), 1, results)

        self.assertEqual(
            await self.count_task(
                ids, "workflow.task_plan_version", where="task_id=:task AND status='ACTIVE'"
            ),
            1,
        )
        self.assertEqual(
            await self.count_task(ids, "workflow.task_plan_version"),
            1,
        )
        # 事件序号只能由一个激活者分配，且不重复。
        self.assertEqual(
            await self.count_task(ids, "workflow.task_event"),
            2,
        )

    async def test_ready_goal_accepts_model_issues_rejected_by_gate(self) -> None:
        ids = new_ids()
        await self.seed_all(ids)
        await self.activate(make_plan_write_set(ids, plan_version=1, predecessor=None))

        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE workflow.task_goal_understanding "
                    "SET blocking_issue_count=1 "
                    "WHERE understanding_id=:goal AND gate_outcome='READY_TO_PLAN'"
                ),
                {"goal": ids["goal"]},
            )
        async with self.engine.connect() as connection:
            issue_count = await connection.scalar(
                text(
                    "SELECT blocking_issue_count FROM workflow.task_goal_understanding "
                    "WHERE understanding_id=:goal"
                ),
                {"goal": ids["goal"]},
            )
        self.assertEqual(issue_count, 1)


if __name__ == "__main__":
    unittest.main()
