"""Goal原子提交写集的纯关系校验。"""

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from knowledge_system.infrastructure.persistence import (
    GoalCompletionWriteSet,
    PersistenceContractError,
)
from knowledge_system.infrastructure.persistence.attempt_models import TaskAttemptRecord
from knowledge_system.infrastructure.persistence.planning_models import (
    TaskGoalUnderstandingRecord,
)
from knowledge_system.infrastructure.persistence.task_models import (
    OutboxMessageRecord,
    TaskEventRecord,
    UserInputRequestRecord,
)

DIGEST = "a" * 64
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_goal(task_id: UUID, input_id: UUID, attempt_id: UUID) -> TaskGoalUnderstandingRecord:
    return TaskGoalUnderstandingRecord(
        understanding_id=uuid4(),
        task_id=task_id,
        input_id=input_id,
        task_attempt_id=attempt_id,
        gate_outcome="READY_TO_PLAN",
    )


def make_attempt(task_id: UUID, input_id: UUID, attempt_id: UUID) -> TaskAttemptRecord:
    return TaskAttemptRecord(
        id=attempt_id,
        task_id=task_id,
        input_id=input_id,
        plan_version=None,
        attempt_kind="GOAL_UNDERSTANDING",
        status="COMPLETED",
    )


def make_event(task_id: UUID, event_type: str) -> TaskEventRecord:
    return TaskEventRecord(id=uuid4(), task_id=task_id, event_type=event_type)


def make_outbox(task_id: UUID, event_id: UUID) -> OutboxMessageRecord:
    return OutboxMessageRecord(
        id=uuid4(), event_id=event_id, aggregate_id=task_id, destination="MQ"
    )


def ready_write_set() -> GoalCompletionWriteSet:
    task_id, input_id, attempt_id = uuid4(), uuid4(), uuid4()
    goal = make_goal(task_id, input_id, attempt_id)
    event = make_event(task_id, "GOAL_UNDERSTANDING_COMPLETED")
    return GoalCompletionWriteSet(
        goal_understanding=goal,
        expected_task_version=1,
        task_attempt=make_attempt(task_id, input_id, attempt_id),
        events=(event,),
        outbox_messages=(make_outbox(task_id, event.id),),
    )


class GoalCompletionWriteSetTests(unittest.TestCase):
    def test_ready_goal_write_set_passes(self) -> None:
        ready_write_set().validate_relations()

    def test_attempt_must_be_completed_goal_attempt_for_same_scope(self) -> None:
        write_set = ready_write_set()
        assert write_set.task_attempt is not None
        write_set.task_attempt.status = "RUNNING"
        with self.assertRaisesRegex(PersistenceContractError, "GOAL_ATTEMPT_NOT_COMPLETED"):
            write_set.validate_relations()

    def test_ready_goal_cannot_create_user_request(self) -> None:
        write_set = ready_write_set()
        goal = write_set.goal_understanding
        request = UserInputRequestRecord(
            id=uuid4(),
            task_id=goal.task_id,
            input_id=goal.input_id,
            source_task_attempt_id=goal.task_attempt_id,
            goal_understanding_id=goal.understanding_id,
            origin="GOAL_CLARIFICATION",
            status="PENDING",
        )
        invalid = GoalCompletionWriteSet(
            goal_understanding=goal,
            expected_task_version=1,
            task_attempt=write_set.task_attempt,
            user_input_request=request,
            events=write_set.events,
            outbox_messages=write_set.outbox_messages,
        )
        with self.assertRaisesRegex(PersistenceContractError, "GOAL_READY_REQUEST_FORBIDDEN"):
            invalid.validate_relations()

    def test_blocked_goal_requires_matching_request_and_event(self) -> None:
        write_set = ready_write_set()
        goal = write_set.goal_understanding
        goal.gate_outcome = "REQUEST_USER_INPUT"
        goal.blocking_issue_count = 1
        with self.assertRaisesRegex(
            PersistenceContractError, "GOAL_CLARIFICATION_REQUEST_REQUIRED"
        ):
            write_set.validate_relations()

        request = UserInputRequestRecord(
            id=uuid4(),
            task_id=goal.task_id,
            input_id=goal.input_id,
            origin="GOAL_CLARIFICATION",
            source_task_attempt_id=goal.task_attempt_id,
            question_artifact_id=uuid4(),
            input_schema_json={},
            response_contract_version="clarification_response_contract_v1",
            display_summary="需要补充信息",
            status="PENDING",
            response_artifact_id=None,
            response_size_bytes=None,
            goal_understanding_id=goal.understanding_id,
            clarification_round_no=1,
            resume_bundle_id=None,
            deferred_call_id=None,
            runtime_blocking_issue_id=None,
            issue_fingerprint=None,
            request_version=1,
            row_version=1,
            requested_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            submitted_at=None,
        )
        completion_event = write_set.events[0]
        request_event = make_event(goal.task_id, "USER_INPUT_REQUESTED")
        valid = GoalCompletionWriteSet(
            goal_understanding=goal,
            expected_task_version=1,
            task_attempt=write_set.task_attempt,
            user_input_request=request,
            events=(completion_event, request_event),
            outbox_messages=(make_outbox(goal.task_id, request_event.id),),
        )
        valid.validate_relations()

    def test_outbox_must_target_same_task(self) -> None:
        write_set = ready_write_set()
        write_set.outbox_messages[0].aggregate_id = uuid4()
        with self.assertRaisesRegex(PersistenceContractError, "GOAL_OUTBOX_TASK_MISMATCH"):
            write_set.validate_relations()


if __name__ == "__main__":
    unittest.main()
