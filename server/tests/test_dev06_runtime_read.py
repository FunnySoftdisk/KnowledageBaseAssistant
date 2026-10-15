"""DEV-06 运行时事实只读仓库验收：`TaskRuntimeReadRepository.load_facts`。

离线部分用可编程 Fake Session 证明「任务缺当前输入/活动计划 → `RuntimeFactsNotRunnableError`」与
「输入快照缺失 → `RuntimeFactsIntegrityError`」两条失败关闭路径；真实 PG 部分
（`KNOWLEDGE_TEST_DATABASE_URL` 未配置时跳过）验证任务行直读、运行时权威投影、终态关闭信号与
`task_result` 派生信号的完整事实装配。

派生自 `task_attempt`/`plan_item_runtime`/`user_input_request` 的活跃信号依赖较重的尝试/计划播种，
其字段名与状态字面量已由 mypy 与 ORM CHECK 约束共同钉死，此处以空派生默认值覆盖。
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4

from dev06_pg_support import (
    DIGEST,
    TEST_DATABASE_URL,
    IsolatedDatabase,
    seed_foundation,
)
from sqlalchemy import text

from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.runtime_state_repository import (
    TaskRuntimeStateRepository,
)
from knowledge_system.infrastructure.persistence.task_models import IntelligentTaskRecord
from knowledge_system.infrastructure.persistence.task_runtime_read import (
    RuntimeFactsIntegrityError,
    RuntimeFactsNotRunnableError,
    TaskRuntimeReadRepository,
)
from knowledge_system.modules.tasking.application.runtime_state import RuntimeLoopStateV1

_UTC = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


def _task_record(
    task_id: UUID,
    *,
    current_input_id: UUID | None,
    active_plan_version: int | None,
) -> IntelligentTaskRecord:
    return IntelligentTaskRecord(
        id=task_id,
        version=1,
        last_event_sequence=0,
        status="PLANNING",
        current_input_id=current_input_id,
        active_plan_version=active_plan_version,
        deadline_at=_UTC,
    )


class _ScalarResult:
    def __init__(self, record: object | None) -> None:
        self._record = record

    def first(self) -> object | None:
        return self._record

    def all(self) -> list[object]:
        return [] if self._record is None else [self._record]


class _QueueSession:
    """按调用顺序返回预置记录，耗尽后返回 None；覆盖只读仓库的 `scalars` 探针。"""

    def __init__(self, records: list[object | None]) -> None:
        self._records = list(records)

    async def scalars(self, statement: object) -> _ScalarResult:
        record = self._records.pop(0) if self._records else None
        return _ScalarResult(record)


class RuntimeReadOfflineTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_task_returns_none(self) -> None:
        repo = TaskRuntimeReadRepository(_QueueSession([]))
        self.assertIsNone(await repo.load_facts(uuid4()))

    async def test_missing_cursor_raises_not_runnable(self) -> None:
        task = _task_record(uuid4(), current_input_id=None, active_plan_version=1)
        repo = TaskRuntimeReadRepository(_QueueSession([task]))
        with self.assertRaises(RuntimeFactsNotRunnableError):
            await repo.load_facts(task.id)

    async def test_missing_plan_raises_not_runnable(self) -> None:
        task = _task_record(uuid4(), current_input_id=uuid4(), active_plan_version=None)
        repo = TaskRuntimeReadRepository(_QueueSession([task]))
        with self.assertRaises(RuntimeFactsNotRunnableError):
            await repo.load_facts(task.id)

    async def test_missing_input_snapshot_raises_integrity(self) -> None:
        task = _task_record(uuid4(), current_input_id=uuid4(), active_plan_version=1)
        repo = TaskRuntimeReadRepository(_QueueSession([task, None]))
        with self.assertRaises(RuntimeFactsIntegrityError):
            await repo.load_facts(task.id)


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class RuntimeReadPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev06_runtime_read_{os.getpid()}_{uuid4().hex[:8]}"
        self.isolated = IsolatedDatabase(TEST_DATABASE_URL, self.dbname)
        self.url = await self.isolated.__aenter__()
        self.engine = build_async_engine(DatabaseEngineSettings(url=self.url))
        self.session_factory = build_session_factory(self.engine)
        self.ids = {
            name: uuid4()
            for name in ("org", "user", "conversation", "task", "message", "input")
        }
        self.artifacts = {
            name: uuid4()
            for name in (
                "query",
                "answer",
                "envelope",
                "candidate",
                "validation",
                "evaluation_set",
                "gate_decision",
            )
        }
        await seed_foundation(
            self.engine, self.ids, self.artifacts, active_plan_version=1
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        await self.isolated.__aexit__(None, None, None)

    async def _load(self):
        async with self.session_factory() as session:
            facts = await TaskRuntimeReadRepository(session).load_facts(self.ids["task"])
        assert facts is not None
        return facts

    async def _set_runtime_state(self, state: RuntimeLoopStateV1) -> None:
        async with self.session_factory() as session:
            await TaskRuntimeStateRepository(session).upsert(self.ids["task"], state)
            await session.commit()

    async def _set_task_status(self, status: str) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                text("UPDATE workflow.intelligent_task SET status=:s WHERE id=:t"),
                {"s": status, "t": self.ids["task"]},
            )

    async def test_base_projection(self) -> None:
        facts = await self._load()
        self.assertEqual(facts.task_id, self.ids["task"])
        self.assertEqual(facts.db_status, "PLANNING")
        self.assertEqual(facts.input_id, self.ids["input"])
        self.assertEqual(facts.input_version, 1)
        self.assertEqual(facts.plan_version, 1)
        self.assertEqual(facts.work_version, 1)
        self.assertFalse(facts.terminal_committed)
        self.assertFalse(facts.runtime_close_ready)
        self.assertFalse(facts.cancellation_fence_required)
        self.assertEqual(facts.retryable_items, ())
        self.assertEqual(facts.wake_sources, ())

    async def test_runtime_state_projection_and_cancellation_fence(self) -> None:
        await self._set_runtime_state(
            RuntimeLoopStateV1(work_version=3, execution_generation=1, cancellation_generation=2)
        )
        facts = await self._load()
        self.assertEqual(facts.work_version, 3)
        self.assertEqual(facts.execution_generation, 1)
        self.assertEqual(facts.cancellation_generation, 2)
        self.assertTrue(facts.cancellation_fence_required)

    async def test_terminal_close_ready(self) -> None:
        await self._set_task_status("COMPLETED")
        facts = await self._load()
        self.assertTrue(facts.terminal_committed)
        self.assertTrue(facts.runtime_close_ready)
        self.assertTrue(facts.snapshot_version_stable)

    async def test_required_criteria_satisfied(self) -> None:
        await self._insert_task_result("COMPLETE")
        facts = await self._load()
        self.assertTrue(facts.required_criteria_satisfied)
        self.assertFalse(facts.has_partial_results)

    async def test_missing_task_returns_none(self) -> None:
        async with self.session_factory() as session:
            self.assertIsNone(await TaskRuntimeReadRepository(session).load_facts(uuid4()))

    async def _insert_task_result(self, answer_status: str) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO workflow.task_result "
                    "(task_id,contract_version,result_version,answer_status,answer_artifact_id,"
                    "answer_sha256,result_envelope_artifact_id,citation_count,result_artifact_count,"
                    "criterion_checks,constraint_checks,degradation_flags,final_snapshot_id,"
                    "final_attempt_id,candidate_artifact_id,validation_artifact_id,"
                    "criterion_evaluation_set_artifact_id,completion_gate_decision_artifact_id,"
                    "renderer_profile,renderer_profile_digest,result_commit_digest,commit_receipt_id,"
                    "generated_at,row_version) VALUES "
                    "(:task,'task_result_v1',1,:status,:answer,:sha,:envelope,3,2,"
                    "'[]'::jsonb,'[]'::jsonb,'{}'::text[],:snapshot,:attempt,:candidate,:validation,"
                    ":evaluation,:gate,'final-answer-markdown-v1',:sha,:digest,:receipt,:generated,1)"
                ),
                {
                    "task": self.ids["task"],
                    "status": answer_status,
                    "answer": self.artifacts["answer"],
                    "sha": DIGEST,
                    "envelope": self.artifacts["envelope"],
                    "snapshot": uuid4(),
                    "attempt": uuid4(),
                    "candidate": self.artifacts["candidate"],
                    "validation": self.artifacts["validation"],
                    "evaluation": self.artifacts["evaluation_set"],
                    "gate": self.artifacts["gate_decision"],
                    "digest": "c" * 64,
                    "receipt": uuid4(),
                    "generated": _UTC,
                },
            )


if __name__ == "__main__":
    unittest.main()
