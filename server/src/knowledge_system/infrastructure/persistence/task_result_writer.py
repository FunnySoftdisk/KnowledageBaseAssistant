"""DEV-06：`workflow.task_result` 终态提交的事务化适配器。

`SqlAlchemyTaskResultWriter` 实现应用层 `TaskResultCommitWriter` 端口：在独立事务边界内调用
`TaskResultRepository.insert_committed` 并提交。幂等语义（INSERTED / IDEMPOTENT_REPLAY /
CONFLICT）由 Repository 承担，本适配器只负责事务边界与提交，不自行重试、不伪造冲突结果；
提交抛出异常时事务回滚，不留半行。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_system.modules.tasking.application.final_commit import (
    TaskResultCommitResult,
    TaskResultCommitV1,
)

from .task_result_repository import TaskResultRepository


class SqlAlchemyTaskResultWriter:
    """把终态提交经独立事务落库；单一 Repository 操作，不引入跨仓库 UoW 编排。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def commit(self, commit: TaskResultCommitV1) -> TaskResultCommitResult:
        async with self._session_factory() as session:
            async with session.begin():
                return await TaskResultRepository(session).insert_committed(commit)


__all__ = ["SqlAlchemyTaskResultWriter"]
