"""DEV-06 任务级运行时闭环权威持久层验收：`workflow.task_runtime_state`。

离线部分证明 `record_from_state`/`state_from_record` 互转确定性、`RuntimeLoopStateV1` 的
REPLAN 预算自洽校验、`state_from_record` 对坏 JSON 的失败关闭，以及 upsert 的 `row_version` CAS
冲突不落盘；真实 PG 部分（`KNOWLEDGE_TEST_DATABASE_URL` 未配置时跳过）验证迁移 0010 建表、
首次 upsert INSERT、重复 upsert 走 CAS 更新、`row_version` 陈旧冲突拒绝，以及全字段往返。
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
from pydantic import ValidationError

from knowledge_system.infrastructure.persistence import (
    Base,
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.infrastructure.persistence.runtime_state_repository import (
    RuntimeStateRecordIntegrityError,
    RuntimeStateRowVersionConflict,
    TaskRuntimeStateRepository,
    record_from_state,
    state_from_record,
)
from knowledge_system.modules.tasking.application.runtime_convergence import (
    RuntimeConvergenceActionV1,
)
from knowledge_system.modules.tasking.application.runtime_state import RuntimeLoopStateV1
from knowledge_system.modules.tasking.domain.core_types import VersionedRefV1
from knowledge_system.modules.tasking.domain.runtime_decision import (
    RuntimeBlockingIssueKindV1,
    RuntimeBlockingIssueV1,
)
from knowledge_system.modules.tasking.domain.runtime_progress import NoProgressStateV1

_UTC = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


def _ref() -> VersionedRefV1:
    return VersionedRefV1(ref_id=uuid4(), version=1, integrity_digest=DIGEST)


def full_state() -> RuntimeLoopStateV1:
    ref = _ref()
    return RuntimeLoopStateV1(
        work_version=3,
        execution_generation=2,
        cancellation_generation=5,
        next_reconcile_at=_UTC,
        blocking_effect_refs=(ref,),
        blocking_issue=RuntimeBlockingIssueV1(
            issue_kind=RuntimeBlockingIssueKindV1.REQUIRED_PARAMETER_MISSING,
            issue_fingerprint="b" * 64,
            refs=(ref,),
        ),
        ready_set_ref=ref,
        no_progress_state=NoProgressStateV1(
            current_loop_fingerprint="c" * 64,
            same_loop_no_progress=1,
            consecutive_no_progress=2,
            epoch_no_progress=3,
            task_no_progress=4,
            episode_replan_used=False,
            task_replan_count=1,
            episode_seen_loop_fingerprints=("c" * 64,),
        ),
        convergence_action=RuntimeConvergenceActionV1.CONTINUE_LOOP,
        replan_required=False,
    )


class _ScalarResult:
    def __init__(self, record: object) -> None:
        self._record = record

    def first(self) -> object:
        return self._record


class _FakeSession:
    """只实现运行时状态仓库用到的 `scalars`/`add`，用于离线 CAS 与冲突路径。"""

    def __init__(self, record: object | None = None) -> None:
        self.record = record

    async def scalars(self, statement: object) -> _ScalarResult:
        return _ScalarResult(self.record)

    def add(self, record: object) -> None:
        self.record = record


class TaskRuntimeStateMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.table = Base.metadata.tables["workflow.task_runtime_state"]

    def test_primary_key_is_task_id(self) -> None:
        self.assertEqual([c.name for c in self.table.primary_key.columns], ["task_id"])

    def test_foreign_key_targets_intelligent_task(self) -> None:
        fks = list(self.table.c["task_id"].foreign_keys)
        self.assertEqual(len(fks), 1)
        self.assertEqual(fks[0].column.table.name, "intelligent_task")

    def test_no_progress_and_convergence_columns_present(self) -> None:
        for column in (
            "work_version",
            "execution_generation",
            "cancellation_generation",
            "same_loop_no_progress",
            "consecutive_no_progress",
            "epoch_no_progress",
            "task_no_progress",
            "episode_replan_used",
            "task_replan_count",
            "convergence_action",
            "replan_required",
            "row_version",
        ):
            self.assertIn(column, self.table.c)


class RuntimeStateMappingTests(unittest.TestCase):
    def test_full_roundtrip(self) -> None:
        task_id = uuid4()
        state = full_state()
        self.assertEqual(state_from_record(record_from_state(task_id, state)), state)

    def test_default_roundtrip(self) -> None:
        task_id = uuid4()
        state = RuntimeLoopStateV1()
        self.assertEqual(state_from_record(record_from_state(task_id, state)), state)

    def test_record_flattens_enums_and_nested_state(self) -> None:
        record = record_from_state(uuid4(), full_state())
        self.assertEqual(record.convergence_action, "CONTINUE_LOOP")
        self.assertEqual(record.same_loop_no_progress, 1)
        self.assertEqual(record.task_no_progress, 4)
        self.assertEqual(record.current_loop_fingerprint, "c" * 64)
        self.assertEqual(record.episode_seen_loop_fingerprints_json, ["c" * 64])

    def test_state_from_record_rejects_non_array_refs(self) -> None:
        record = record_from_state(uuid4(), RuntimeLoopStateV1())
        record.blocking_effect_refs_json = {"not": "a list"}  # type: ignore[assignment]
        with self.assertRaises(RuntimeStateRecordIntegrityError):
            state_from_record(record)

    def test_state_from_record_rejects_non_object_issue(self) -> None:
        record = record_from_state(uuid4(), RuntimeLoopStateV1())
        record.blocking_issue_json = ["not", "an", "object"]  # type: ignore[assignment]
        with self.assertRaises(RuntimeStateRecordIntegrityError):
            state_from_record(record)


class RuntimeLoopStateContractTests(unittest.TestCase):
    def test_default_is_constructible(self) -> None:
        state = RuntimeLoopStateV1()
        self.assertEqual(state.work_version, 1)
        self.assertEqual(state.convergence_action, RuntimeConvergenceActionV1.CONTINUE_LOOP)
        self.assertFalse(state.replan_required)

    def test_replan_requires_episode_marked(self) -> None:
        with self.assertRaises(ValidationError):
            RuntimeLoopStateV1(
                convergence_action=RuntimeConvergenceActionV1.REPLAN,
                no_progress_state=NoProgressStateV1(
                    episode_replan_used=False, task_replan_count=1
                ),
            )

    def test_replan_requires_budget_in_range(self) -> None:
        with self.assertRaises(ValidationError):
            RuntimeLoopStateV1(
                convergence_action=RuntimeConvergenceActionV1.REPLAN,
                no_progress_state=NoProgressStateV1(
                    episode_replan_used=True, task_replan_count=0
                ),
            )


class RuntimeStateRepositoryOfflineTests(unittest.IsolatedAsyncioTestCase):
    async def test_cas_conflict_raises_without_mutation(self) -> None:
        task_id = uuid4()
        existing = record_from_state(task_id, full_state())
        existing.row_version = 3
        repo = TaskRuntimeStateRepository(_FakeSession(existing))
        with self.assertRaises(RuntimeStateRowVersionConflict):
            await repo.upsert(task_id, full_state(), expected_row_version=1)
        self.assertEqual(existing.row_version, 3)


@unittest.skipUnless(TEST_DATABASE_URL, "KNOWLEDGE_TEST_DATABASE_URL is not configured")
class RuntimeStatePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert TEST_DATABASE_URL is not None
        self.dbname = f"dev06_runtime_state_{os.getpid()}_{uuid4().hex[:8]}"
        self.isolated = IsolatedDatabase(TEST_DATABASE_URL, self.dbname)
        self.url = await self.isolated.__aenter__()
        self.engine = build_async_engine(DatabaseEngineSettings(url=self.url))
        self.session_factory = build_session_factory(self.engine)
        self.ids = {
            name: uuid4()
            for name in ("org", "user", "conversation", "task", "message", "input")
        }
        await seed_foundation(self.engine, self.ids, {"query": uuid4()})

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        await self.isolated.__aexit__(None, None, None)

    async def _upsert(self, task_id: UUID, state: RuntimeLoopStateV1, **kwargs):
        async with self.session_factory() as session:
            result = await TaskRuntimeStateRepository(session).upsert(
                task_id, state, **kwargs
            )
            await session.commit()
        return result

    async def _row_version(self, task_id: UUID) -> int:
        async with self.session_factory() as session:
            from sqlalchemy import text

            row = (
                await session.execute(
                    text(
                        "SELECT row_version FROM workflow.task_runtime_state WHERE task_id=:t"
                    ),
                    {"t": task_id},
                )
            ).scalar_one()
            return int(row)

    async def test_first_upsert_inserts(self) -> None:
        task_id = self.ids["task"]
        state = full_state()
        loaded, created = await self._upsert(task_id, state)
        self.assertTrue(created)
        self.assertEqual(loaded, state)
        self.assertEqual(await self._row_version(task_id), 1)

    async def test_second_upsert_cas_updates(self) -> None:
        task_id = self.ids["task"]
        await self._upsert(task_id, full_state())
        updated = full_state().model_copy(update={"work_version": 4})
        loaded, created = await self._upsert(task_id, updated, expected_row_version=1)
        self.assertFalse(created)
        self.assertEqual(loaded.work_version, 4)
        self.assertEqual(await self._row_version(task_id), 2)

    async def test_stale_row_version_conflicts(self) -> None:
        task_id = self.ids["task"]
        await self._upsert(task_id, full_state())
        with self.assertRaises(RuntimeStateRowVersionConflict):
            await self._upsert(task_id, full_state(), expected_row_version=7)
        self.assertEqual(await self._row_version(task_id), 1)

    async def test_get_returns_none_for_missing(self) -> None:
        async with self.session_factory() as session:
            self.assertIsNone(await TaskRuntimeStateRepository(session).get(uuid4()))


if __name__ == "__main__":
    unittest.main()
