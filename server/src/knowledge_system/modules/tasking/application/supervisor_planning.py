"""DEV-05：Supervisor候选生成编排——Goal理解与首轮Plan Draft。

服务只做候选装配与确定性门禁：`GoalUnderstandingService`把模型候选装配为完整理解、先做
语义引用校验（非法引用稳定拒绝）再走确定性Goal Gate；`InitialPlanService`把候选装配为首轮
Draft（验证/编译由已落地的`validate_plan`/`compile_plan`负责）。两者都不做I/O、不持有Session。
"""

from __future__ import annotations

from knowledge_system.modules.tasking.domain.goal_contracts import (
    GoalGateDecisionV1,
    GoalUnderstandingContextV1,
    validate_goal_relations,
)
from knowledge_system.modules.tasking.domain.goal_gate import decide_goal_gate
from knowledge_system.modules.tasking.domain.planning_fragments import (
    InitialPlanDraftV1,
    InitialPlanningContextV1,
)
from knowledge_system.modules.tasking.domain.supervisor_candidates import (
    assemble_goal_understanding,
    assemble_initial_plan,
)

from .errors import TaskEntryError
from .supervisor_model import SupervisorModelPort


class GoalUnderstandingService:
    """Goal理解候选→装配→语义校验→Goal Gate。"""

    def __init__(self, model: SupervisorModelPort) -> None:
        self._model = model

    async def understand(self, context: GoalUnderstandingContextV1) -> GoalGateDecisionV1:
        output = await self._model.generate_goal_understanding(context)
        goal = assemble_goal_understanding(output, context)
        if validate_goal_relations(goal, context):
            raise TaskEntryError("GOAL_UNDERSTANDING_SEMANTIC_INVALID")
        return decide_goal_gate(goal)


class InitialPlanService:
    """首轮Plan候选→装配为Draft；验证/编译留给`validate_plan`。"""

    def __init__(self, model: SupervisorModelPort) -> None:
        self._model = model

    async def draft(self, context: InitialPlanningContextV1) -> InitialPlanDraftV1:
        output = await self._model.generate_initial_plan(context)
        return assemble_initial_plan(output, context)


__all__ = ["GoalUnderstandingService", "InitialPlanService"]
