"""DEV-07 后端离线契约：Task Event 游标、SSE 重连、终态查询与断线恢复。

不依赖运行中的 FastAPI/SSE/Temporal，验证：
- 任务状态枚举与终态/开放态分类；
- `Last-Event-ID` 游标解析的失败关闭；
- 可流式事件白名单与授权负载投影（Secret/Digest/内部字段禁止进入）；
- 断线恢复判定（终态先渲染、开放态按游标续读、游标越界失败关闭）；
- 失败与取消绝不显示为成功。
"""

from __future__ import annotations

import unittest
from uuid import UUID

from pydantic import ValidationError

from knowledge_system.modules.tasking.domain.event_stream import (
    OPEN_STATUSES,
    SSE_STREAMABLE_EVENT_TYPES,
    TERMINAL_STATUSES,
    ReconnectDecisionV1,
    RecoverActionV1,
    SSEStreamError,
    TaskEventCursorV1,
    TaskEventProjectionV1,
    TaskStatusV1,
    TerminalDisplayV1,
    decide_reconnect,
    is_forbidden_projection_key,
    parse_event_cursor,
    project_stream_event,
    terminal_display_for,
)

_TASK_ID = UUID("5f0a7e8c-3d1b-4a2f-9c6e-1b2d3e4f5a6b")


class TaskStatusClassificationTest(unittest.TestCase):
    def test_all_twelve_statuses_are_classified(self) -> None:
        self.assertEqual(len(TaskStatusV1), 12)

    def test_terminal_statuses_exactly_three(self) -> None:
        self.assertEqual(TERMINAL_STATUSES, frozenset({"COMPLETED", "FAILED", "CANCELLED"}))

    def test_open_statuses_are_the_remaining_nine(self) -> None:
        self.assertEqual(len(OPEN_STATUSES), 9)
        self.assertTrue(TERMINAL_STATUSES.isdisjoint(OPEN_STATUSES))
        self.assertEqual(OPEN_STATUSES | TERMINAL_STATUSES, {s.value for s in TaskStatusV1})

    def test_is_terminal_and_is_open_flags(self) -> None:
        for status in TaskStatusV1:
            with self.subTest(status=status.value):
                self.assertEqual(status.is_terminal, status.value in TERMINAL_STATUSES)
                self.assertEqual(status.is_open, status.value in OPEN_STATUSES)
                self.assertNotEqual(status.is_terminal, status.is_open)


class EventCursorParseTest(unittest.TestCase):
    def test_missing_or_blank_is_fresh(self) -> None:
        self.assertEqual(parse_event_cursor(None).sequence, 0)
        self.assertEqual(parse_event_cursor("").sequence, 0)
        self.assertEqual(parse_event_cursor("   ").sequence, 0)

    def test_zero_and_positive_values(self) -> None:
        self.assertEqual(parse_event_cursor("0").sequence, 0)
        self.assertEqual(parse_event_cursor("7").sequence, 7)
        self.assertEqual(parse_event_cursor(" 7 ").sequence, 7)

    def test_resume_from_sequence_is_cursor_plus_one(self) -> None:
        self.assertEqual(parse_event_cursor("0").resume_from_sequence, 1)
        self.assertEqual(parse_event_cursor("7").resume_from_sequence, 8)

    def test_malformed_values_fail_closed(self) -> None:
        for raw in ("abc", "-1", "1.5", "+1", "2^63", "999999999999999999999999"):
            with self.subTest(raw=raw):
                with self.assertRaises(SSEStreamError) as ctx:
                    parse_event_cursor(raw)
                self.assertEqual(ctx.exception.code, "SSE_CURSOR_INVALID")

    def test_over_2_63_fails_closed(self) -> None:
        with self.assertRaises(SSEStreamError) as ctx:
            parse_event_cursor(str(2**63))
        self.assertEqual(ctx.exception.code, "SSE_CURSOR_INVALID")


class TerminalDisplayTest(unittest.TestCase):
    def test_completed_is_success(self) -> None:
        self.assertIs(terminal_display_for(TaskStatusV1.COMPLETED), TerminalDisplayV1.SUCCESS)

    def test_failed_and_cancelled_never_success(self) -> None:
        self.assertIs(terminal_display_for(TaskStatusV1.FAILED), TerminalDisplayV1.FAILED)
        self.assertIs(terminal_display_for(TaskStatusV1.CANCELLED), TerminalDisplayV1.CANCELLED)
        self.assertNotIn(terminal_display_for(TaskStatusV1.FAILED), (TerminalDisplayV1.SUCCESS,))
        self.assertNotIn(terminal_display_for(TaskStatusV1.CANCELLED), (TerminalDisplayV1.SUCCESS,))

    def test_non_terminal_status_rejected(self) -> None:
        for status in TaskStatusV1:
            if status.is_terminal:
                continue
            with self.subTest(status=status.value):
                with self.assertRaises(SSEStreamError) as ctx:
                    terminal_display_for(status)
                self.assertEqual(ctx.exception.code, "SSE_TERMINAL_STATUS_REQUIRED")


class ForbiddenProjectionKeyTest(unittest.TestCase):
    def test_secret_names_forbidden_case_insensitive(self) -> None:
        for name in ("api_key", "API_KEY", "Authorization", "secret_ref", "BEARER"):
            with self.subTest(name=name):
                self.assertTrue(is_forbidden_projection_key(name))

    def test_digest_and_sha256_suffixes_forbidden(self) -> None:
        self.assertTrue(is_forbidden_projection_key("payload_digest"))
        self.assertTrue(is_forbidden_projection_key("snapshot_sha256"))

    def test_safe_progress_fields_allowed(self) -> None:
        for name in ("task_id", "status", "plan_version", "reason_code", "display_summary"):
            with self.subTest(name=name):
                self.assertFalse(is_forbidden_projection_key(name))


class StreamProjectionTest(unittest.TestCase):
    def test_non_streamable_event_is_not_projected(self) -> None:
        result = project_stream_event(
            sequence=3,
            event_type="OBSERVATION_SET_COMMITTED",
            event_schema_version="task_event_v1",
            payload={},
        )
        self.assertIsNone(result)

    def test_streamable_event_projects_bounded_payload(self) -> None:
        result = project_stream_event(
            sequence=1,
            event_type="TASK_CREATED",
            event_schema_version="task_event_v1",
            payload={"task_id": str(_TASK_ID), "status": "QUEUED"},
        )
        self.assertIsInstance(result, TaskEventProjectionV1)
        self.assertEqual(result.sequence, 1)
        self.assertEqual(result.event_type, "TASK_CREATED")
        self.assertEqual(result.payload.task_id, _TASK_ID)
        self.assertIs(result.payload.status, TaskStatusV1.QUEUED)

    def test_empty_payload_projects_all_none(self) -> None:
        result = project_stream_event(
            sequence=2,
            event_type="PLAN_ACTIVATED",
            event_schema_version="task_event_v1",
            payload={},
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIsNone(result.payload.status)
        self.assertIsNone(result.payload.plan_version)

    def test_secret_field_fails_closed(self) -> None:
        with self.assertRaises(SSEStreamError) as ctx:
            project_stream_event(
                sequence=1,
                event_type="TASK_CREATED",
                event_schema_version="task_event_v1",
                payload={"api_key": "x"},
            )
        self.assertEqual(ctx.exception.code, "SSE_FORBIDDEN_PAYLOAD_FIELD")

    def test_digest_field_fails_closed(self) -> None:
        with self.assertRaises(SSEStreamError) as ctx:
            project_stream_event(
                sequence=1,
                event_type="TASK_CREATED",
                event_schema_version="task_event_v1",
                payload={"payload_digest": "a" * 64},
            )
        self.assertEqual(ctx.exception.code, "SSE_FORBIDDEN_PAYLOAD_FIELD")

    def test_unknown_internal_field_fails_closed(self) -> None:
        with self.assertRaises(SSEStreamError) as ctx:
            project_stream_event(
                sequence=1,
                event_type="TASK_CREATED",
                event_schema_version="task_event_v1",
                payload={"candidate_artifact_id": str(_TASK_ID)},
            )
        self.assertEqual(ctx.exception.code, "SSE_PROJECTION_FIELD_NOT_ALLOWED")


class ReconnectDecisionTest(unittest.TestCase):
    def test_terminal_completed_renders_success(self) -> None:
        decision = decide_reconnect(
            status=TaskStatusV1.COMPLETED,
            server_last_event_sequence=5,
            cursor=TaskEventCursorV1(sequence=5),
        )
        self.assertIs(decision.action, RecoverActionV1.RENDER_RESULT)
        self.assertIs(decision.terminal_display, TerminalDisplayV1.SUCCESS)
        self.assertFalse(decision.heartbeat_required)

    def test_terminal_failed_and_cancelled_never_success(self) -> None:
        for status, display in (
            (TaskStatusV1.FAILED, TerminalDisplayV1.FAILED),
            (TaskStatusV1.CANCELLED, TerminalDisplayV1.CANCELLED),
        ):
            with self.subTest(status=status.value):
                decision = decide_reconnect(
                    status=status,
                    server_last_event_sequence=5,
                    cursor=TaskEventCursorV1(sequence=5),
                )
                self.assertIs(decision.action, RecoverActionV1.RENDER_RESULT)
                self.assertIs(decision.terminal_display, display)
                self.assertIsNot(decision.terminal_display, TerminalDisplayV1.SUCCESS)

    def test_open_fresh_resumes_from_one(self) -> None:
        decision = decide_reconnect(
            status=TaskStatusV1.RUNNING,
            server_last_event_sequence=5,
            cursor=TaskEventCursorV1(sequence=0),
        )
        self.assertIs(decision.action, RecoverActionV1.STREAM_RESUME)
        self.assertEqual(decision.resume_from_sequence, 1)
        self.assertFalse(decision.heartbeat_required)

    def test_open_caught_up_requires_heartbeat(self) -> None:
        decision = decide_reconnect(
            status=TaskStatusV1.RUNNING,
            server_last_event_sequence=5,
            cursor=TaskEventCursorV1(sequence=5),
        )
        self.assertEqual(decision.resume_from_sequence, 6)
        self.assertTrue(decision.heartbeat_required)

    def test_open_behind_resumes_without_heartbeat(self) -> None:
        decision = decide_reconnect(
            status=TaskStatusV1.RUNNING,
            server_last_event_sequence=5,
            cursor=TaskEventCursorV1(sequence=3),
        )
        self.assertEqual(decision.resume_from_sequence, 4)
        self.assertFalse(decision.heartbeat_required)

    def test_cursor_ahead_of_server_fails_closed(self) -> None:
        with self.assertRaises(SSEStreamError) as ctx:
            decide_reconnect(
                status=TaskStatusV1.RUNNING,
                server_last_event_sequence=5,
                cursor=TaskEventCursorV1(sequence=6),
            )
        self.assertEqual(ctx.exception.code, "SSE_CURSOR_AHEAD_OF_SERVER")


class ReconnectDecisionShapeTest(unittest.TestCase):
    def test_render_result_must_not_carry_resume_sequence(self) -> None:
        with self.assertRaises(ValidationError):
            ReconnectDecisionV1(
                action=RecoverActionV1.RENDER_RESULT,
                terminal_display=TerminalDisplayV1.SUCCESS,
                resume_from_sequence=2,
            )

    def test_stream_resume_requires_resume_sequence(self) -> None:
        with self.assertRaises(ValidationError):
            ReconnectDecisionV1(
                action=RecoverActionV1.STREAM_RESUME,
                heartbeat_required=True,
            )

    def test_streamable_event_types_are_exactly_six(self) -> None:
        self.assertEqual(
            SSE_STREAMABLE_EVENT_TYPES,
            frozenset(
                {
                    "TASK_CREATED",
                    "GOAL_UNDERSTANDING_COMPLETED",
                    "PLAN_ACTIVATED",
                    "USER_INPUT_REQUESTED",
                    "TASK_FAILED",
                    "TASK_CANCELLED",
                }
            ),
        )


if __name__ == "__main__":
    unittest.main()
