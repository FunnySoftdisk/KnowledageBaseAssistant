"""DEV-05：Supervisor模型候选生成端口。

业务只依赖此抽象：给定已冻结Context，返回模型生成的候选内容子集。结构修复（≤2次）与
Provider/Secret/网关细节由基础设施层适配器承担；端口不暴露模型、Prompt或网络。
"""

from __future__ import annotations

from typing import Protocol

from knowledge_system.modules.tasking.domain.goal_contracts import GoalUnderstandingContextV1
from knowledge_system.modules.tasking.domain.planning_fragments import InitialPlanningContextV1
from knowledge_system.modules.tasking.domain.supervisor_candidates import (
    GoalUnderstandingModelOutputV1,
    InitialPlanModelOutputV1,
)


class SupervisorModelPort(Protocol):
    async def generate_goal_understanding(
        self, context: GoalUnderstandingContextV1
    ) -> GoalUnderstandingModelOutputV1: ...

    async def generate_initial_plan(
        self, context: InitialPlanningContextV1
    ) -> InitialPlanModelOutputV1: ...


__all__ = ["SupervisorModelPort"]
