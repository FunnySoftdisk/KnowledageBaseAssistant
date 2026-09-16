"""Plan激活写集关系校验的纯逻辑测试（不依赖数据库）。"""

import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4

from knowledge_system.infrastructure.persistence import (
    PersistenceContractError,
    PlanActivationWriteSet,
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
)

_ACTIVATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _plan(
    task_id: UUID,
    plan_version: int,
    *,
    predecessor: int | None = None,
    status: str = "ACTIVE",
    activated_at: datetime | None = _ACTIVATED_AT,
    completed_at: datetime | None = None,
) -> TaskPlanVersionRecord:
    return TaskPlanVersionRecord(
        task_id=task_id,
        plan_version=plan_version,
        predecessor_version=predecessor,
        status=status,
        activated_at=activated_at,
        completed_at=completed_at,
    )


def _item(task_id: UUID, plan_version: int, item_id: UUID) -> TaskPlanItemDefinitionRecord:
    return TaskPlanItemDefinitionRecord(
        task_id=task_id,
        plan_version=plan_version,
        plan_item_id=item_id,
    )


def _runtime(task_id: UUID, plan_version: int, item_id: UUID) -> PlanItemRuntimeRecord:
    return PlanItemRuntimeRecord(
        task_id=task_id,
        plan_version=plan_version,
        plan_item_id=item_id,
    )


_DIGEST = "a" * 64


def _attempt(task_id: UUID, attempt_id: UUID, *, kind: str = "PLAN") -> TaskAttemptRecord:
    return TaskAttemptRecord(
        id=attempt_id,
        task_id=task_id,
        input_id=uuid4(),
        plan_version=1 if kind != "GOAL_UNDERSTANDING" else None,
        attempt_kind=kind,
        attempt_no=1,
        conversation_id=uuid4(),
        agent_instance_id=uuid4(),
        agent_registration_key="supervisor",
        manifest_id="supervisor",
        manifest_version="v1",
        manifest_digest=_DIGEST,
        context_pack_id=uuid4(),
        context_pack_hash=_DIGEST,
        grant_snapshot_digest=_DIGEST,
        input_schema_version="goal-input-v1",
        input_digest=_DIGEST,
        pydantic_run_id=f"run-{attempt_id}",
        temporal_child_workflow_id=f"agent-attempt-v1-{task_id}-{attempt_id}",
        status="RUNNING",
        external_effect_state="NO_EFFECT",
    )


def _goal(task_id: UUID, understanding_id: UUID) -> TaskGoalUnderstandingRecord:
    return TaskGoalUnderstandingRecord(
        understanding_id=understanding_id,
        task_id=task_id,
        input_id=uuid4(),
        task_attempt_id=uuid4(),
        contract_version="supervisor_goal_understanding_v1",
        prompt_profile="supervisor-goal-understanding-v1",
        prompt_profile_digest=_DIGEST,
        model_alias="planning_strong",
        goal_context_artifact_id=uuid4(),
        context_digest=_DIGEST,
        raw_model_artifact_id=uuid4(),
        goal_artifact_id=uuid4(),
        understanding_digest=_DIGEST,
        gate_profile="clarification_gate_v1",
        gate_profile_digest=_DIGEST,
        gate_artifact_id=uuid4(),
        gate_digest=_DIGEST,
        gate_outcome="READY_TO_PLAN",
        subgoal_count=1,
        criterion_count=1,
        assumption_count=0,
        blocking_issue_count=0,
    )


def _event(task_id: UUID, event_id: UUID) -> TaskEventRecord:
    return TaskEventRecord(
        id=event_id,
        task_id=task_id,
        sequence=0,
        event_type="PLAN_ACTIVATED",
        event_schema_version="task-event-v1",
        workflow_relevant=False,
        observation_expected_count=0,
        payload_json={},
        payload_digest=_DIGEST,
    )


def _outbox(event_id: UUID, *, destination: str = "MQ") -> OutboxMessageRecord:
    return OutboxMessageRecord(
        id=uuid4(),
        event_id=event_id,
        aggregate_type="TASK",
        aggregate_id=uuid4(),
        aggregate_version=1,
        destination=destination,
        schema_version="outbox-v1",
        payload_json={},
        payload_digest=_DIGEST,
        publish_status="PENDING",
        available_at=_ACTIVATED_AT,
        attempt_count=0,
        row_version=1,
    )


class PlanActivationWriteSetTests(unittest.TestCase):
    def test_empty_write_set_has_expected_shape(self) -> None:
        write_set = PlanActivationWriteSet(plan_version=_plan(uuid4(), 1))
        self.assertEqual(write_set.items, ())
        self.assertEqual(write_set.hypotheses, ())
        self.assertEqual(write_set.criteria, ())
        self.assertEqual(write_set.runtime_rows, ())
        write_set.validate_relations()  # 空写集是合法的最小激活形态。

    def test_valid_write_set_with_item_and_runtime_passes(self) -> None:
        task_id = uuid4()
        item_id = uuid4()
        write_set = PlanActivationWriteSet(
            plan_version=_plan(task_id, 2, predecessor=1),
            items=(_item(task_id, 2, item_id),),
            runtime_rows=(_runtime(task_id, 2, item_id),),
        )
        write_set.validate_relations()

    def test_status_must_be_active(self) -> None:
        write_set = PlanActivationWriteSet(plan_version=_plan(uuid4(), 1, status="SUPERSEDED"))
        with self.assertRaisesRegex(PersistenceContractError, "PLAN_ACTIVATION_STATUS_NOT_ACTIVE"):
            write_set.validate_relations()

    def test_activated_at_must_be_present(self) -> None:
        write_set = PlanActivationWriteSet(plan_version=_plan(uuid4(), 1, activated_at=None))
        with self.assertRaisesRegex(PersistenceContractError, "PLAN_ACTIVATION_TIME_SHAPE_INVALID"):
            write_set.validate_relations()

    def test_completed_at_must_be_absent(self) -> None:
        write_set = PlanActivationWriteSet(
            plan_version=_plan(uuid4(), 1, completed_at=_ACTIVATED_AT)
        )
        with self.assertRaisesRegex(PersistenceContractError, "PLAN_ACTIVATION_TIME_SHAPE_INVALID"):
            write_set.validate_relations()

    def test_predecessor_must_precede_plan(self) -> None:
        write_set = PlanActivationWriteSet(plan_version=_plan(uuid4(), 2, predecessor=2))
        with self.assertRaisesRegex(PersistenceContractError, "PLAN_PREDECESSOR_INVALID"):
            write_set.validate_relations()

    def test_child_task_mismatch_rejected(self) -> None:
        task_id = uuid4()
        item_id = uuid4()
        write_set = PlanActivationWriteSet(
            plan_version=_plan(task_id, 1),
            items=(_item(uuid4(), 1, item_id),),
            runtime_rows=(_runtime(task_id, 1, item_id),),
        )
        with self.assertRaisesRegex(
            PersistenceContractError, "PLAN_CHILD_TASK_OR_VERSION_MISMATCH"
        ):
            write_set.validate_relations()

    def test_child_version_mismatch_rejected(self) -> None:
        task_id = uuid4()
        item_id = uuid4()
        write_set = PlanActivationWriteSet(
            plan_version=_plan(task_id, 1),
            items=(_item(task_id, 1, item_id),),
            runtime_rows=(_runtime(task_id, 2, item_id),),
        )
        with self.assertRaisesRegex(
            PersistenceContractError, "PLAN_CHILD_TASK_OR_VERSION_MISMATCH"
        ):
            write_set.validate_relations()

    def test_duplicate_item_id_rejected(self) -> None:
        task_id = uuid4()
        item_id = uuid4()
        write_set = PlanActivationWriteSet(
            plan_version=_plan(task_id, 1),
            items=(_item(task_id, 1, item_id), _item(task_id, 1, item_id)),
            runtime_rows=(_runtime(task_id, 1, item_id),),
        )
        with self.assertRaisesRegex(PersistenceContractError, "PLAN_ITEM_ID_DUPLICATE"):
            write_set.validate_relations()

    def test_runtime_coverage_mismatch_rejected(self) -> None:
        task_id = uuid4()
        item_id = uuid4()
        write_set = PlanActivationWriteSet(
            plan_version=_plan(task_id, 1),
            items=(_item(task_id, 1, item_id),),
            runtime_rows=(_runtime(task_id, 1, uuid4()),),
        )
        with self.assertRaisesRegex(
            PersistenceContractError, "PLAN_RUNTIME_ITEM_COVERAGE_MISMATCH"
        ):
            write_set.validate_relations()

    def test_duplicate_runtime_item_rejected(self) -> None:
        task_id = uuid4()
        item_id = uuid4()
        write_set = PlanActivationWriteSet(
            plan_version=_plan(task_id, 1),
            items=(_item(task_id, 1, item_id),),
            runtime_rows=(_runtime(task_id, 1, item_id), _runtime(task_id, 1, item_id)),
        )
        with self.assertRaisesRegex(PersistenceContractError, "PLAN_RUNTIME_ITEM_DUPLICATE"):
            write_set.validate_relations()

    def test_full_write_set_with_goal_attempt_event_outbox_passes(self) -> None:
        task_id = uuid4()
        attempt_id = uuid4()
        goal_id = uuid4()
        input_id = uuid4()
        agent_id = uuid4()
        item_id = uuid4()
        event_id = uuid4()
        plan = TaskPlanVersionRecord(
            task_id=task_id,
            plan_version=1,
            predecessor_version=None,
            task_attempt_id=attempt_id,
            goal_understanding_id=goal_id,
            input_id=input_id,
            created_by_agent_instance_id=agent_id,
            status="ACTIVE",
            activated_at=_ACTIVATED_AT,
            completed_at=None,
        )
        attempt = _attempt(task_id, attempt_id)
        attempt.input_id = input_id
        attempt.agent_instance_id = agent_id
        goal = _goal(task_id, goal_id)
        goal.input_id = input_id
        write_set = PlanActivationWriteSet(
            plan_version=plan,
            task_attempt=attempt,
            goal_understanding=goal,
            items=(_item(task_id, 1, item_id),),
            runtime_rows=(_runtime(task_id, 1, item_id),),
            events=(_event(task_id, event_id),),
            outbox_messages=(_outbox(event_id),),
        )
        write_set.validate_relations()

    def test_attempt_id_mismatch_rejected(self) -> None:
        task_id = uuid4()
        plan = TaskPlanVersionRecord(
            task_id=task_id,
            plan_version=1,
            task_attempt_id=uuid4(),
            goal_understanding_id=uuid4(),
            status="ACTIVE",
            activated_at=_ACTIVATED_AT,
            completed_at=None,
        )
        write_set = PlanActivationWriteSet(
            plan_version=plan,
            task_attempt=_attempt(task_id, uuid4()),
        )
        with self.assertRaisesRegex(PersistenceContractError, "PLAN_ATTEMPT_ID_MISMATCH"):
            write_set.validate_relations()

    def test_attempt_task_mismatch_rejected(self) -> None:
        task_id = uuid4()
        attempt_id = uuid4()
        plan = TaskPlanVersionRecord(
            task_id=task_id,
            plan_version=1,
            task_attempt_id=attempt_id,
            goal_understanding_id=uuid4(),
            status="ACTIVE",
            activated_at=_ACTIVATED_AT,
            completed_at=None,
        )
        write_set = PlanActivationWriteSet(
            plan_version=plan,
            task_attempt=_attempt(uuid4(), attempt_id),
        )
        with self.assertRaisesRegex(PersistenceContractError, "PLAN_ATTEMPT_TASK_MISMATCH"):
            write_set.validate_relations()

    def test_goal_id_mismatch_rejected(self) -> None:
        task_id = uuid4()
        plan = TaskPlanVersionRecord(
            task_id=task_id,
            plan_version=1,
            task_attempt_id=uuid4(),
            goal_understanding_id=uuid4(),
            status="ACTIVE",
            activated_at=_ACTIVATED_AT,
            completed_at=None,
        )
        write_set = PlanActivationWriteSet(
            plan_version=plan,
            goal_understanding=_goal(task_id, uuid4()),
        )
        with self.assertRaisesRegex(PersistenceContractError, "PLAN_GOAL_ID_MISMATCH"):
            write_set.validate_relations()

    def test_event_task_mismatch_rejected(self) -> None:
        task_id = uuid4()
        write_set = PlanActivationWriteSet(
            plan_version=_plan(task_id, 1),
            events=(_event(uuid4(), uuid4()),),
        )
        with self.assertRaisesRegex(PersistenceContractError, "PLAN_EVENT_TASK_MISMATCH"):
            write_set.validate_relations()

    def test_duplicate_event_id_rejected(self) -> None:
        task_id = uuid4()
        event_id = uuid4()
        write_set = PlanActivationWriteSet(
            plan_version=_plan(task_id, 1),
            events=(_event(task_id, event_id), _event(task_id, event_id)),
        )
        with self.assertRaisesRegex(PersistenceContractError, "PLAN_EVENT_ID_DUPLICATE"):
            write_set.validate_relations()

    def test_outbox_unknown_event_rejected(self) -> None:
        task_id = uuid4()
        write_set = PlanActivationWriteSet(
            plan_version=_plan(task_id, 1),
            events=(_event(task_id, uuid4()),),
            outbox_messages=(_outbox(uuid4()),),
        )
        with self.assertRaisesRegex(PersistenceContractError, "PLAN_OUTBOX_EVENT_UNKNOWN"):
            write_set.validate_relations()

    def test_outbox_duplicate_rejected(self) -> None:
        task_id = uuid4()
        event_id = uuid4()
        write_set = PlanActivationWriteSet(
            plan_version=_plan(task_id, 1),
            events=(_event(task_id, event_id),),
            outbox_messages=(_outbox(event_id), _outbox(event_id)),
        )
        with self.assertRaisesRegex(PersistenceContractError, "PLAN_OUTBOX_DUPLICATE"):
            write_set.validate_relations()


if __name__ == "__main__":
    unittest.main()
