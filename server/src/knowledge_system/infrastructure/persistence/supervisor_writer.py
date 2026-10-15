"""DEV-05：Goal/Plan 写集的事务化提交适配器。

`SqlAlchemyGoalPlanWriter` 实现应用层的 `GoalPlanWriter` 端口：每个写集在独立 UoW 内
执行并提交。Goal 提交与 Plan 激活分属两个事务，满足「Goal 先行落库、Plan 激活时读已提交
Goal」的前置依赖，同时保持 CAS 的原子性边界。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_system.infrastructure.persistence import (
    GoalCompletionWriteSet,
    PlanActivationWriteSet,
    SqlAlchemyUnitOfWork,
)


class SqlAlchemyGoalPlanWriter:
    """把 Goal/Plan 写集经 UoW 落库并提交；不新建事务语义，只做边界与提交。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def complete_goal_understanding(self, write_set: GoalCompletionWriteSet) -> None:
        async with SqlAlchemyUnitOfWork(self._session_factory) as unit_of_work:
            await unit_of_work.plans.complete_goal_understanding(write_set)
            await unit_of_work.commit()

    async def activate_plan_version(self, write_set: PlanActivationWriteSet) -> None:
        async with SqlAlchemyUnitOfWork(self._session_factory) as unit_of_work:
            await unit_of_work.plans.activate_plan_version(write_set)
            await unit_of_work.commit()


__all__ = ["SqlAlchemyGoalPlanWriter"]
