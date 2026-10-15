"""DEV-05：`SupervisorModelPort`的PydanticAI适配器。

把冻结Context序列化为确定性Prompt，以`NativeOutput(strict=True)`跑PydanticAI Agent，
`retries=STRUCTURAL_REPAIR_LIMIT`（=2）即「最多两次结构修复」——输出解析失败时PydanticAI
把校验错误回喂模型重试，超出预算以`UnexpectedModelBehavior`失败关闭，不静默放宽Schema。

模型通过构造注入：离线验收注入`FunctionModel`测试替身，真实运行注入经Secret交接的Provider
模型；适配器不持有密钥、不选择Provider、不做授权。OTel/Phoenix观测接线属DEV-04退出项，
故`instrument=False`。
"""

from __future__ import annotations

import json

from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models import Model

from knowledge_system.modules.tasking.domain.goal_contracts import GoalUnderstandingContextV1
from knowledge_system.modules.tasking.domain.planning_fragments import InitialPlanningContextV1
from knowledge_system.modules.tasking.domain.supervisor_candidates import (
    GoalUnderstandingModelOutputV1,
    InitialPlanModelOutputV1,
)
from knowledge_system.modules.tasking.domain.supervisor_profiles import STRUCTURAL_REPAIR_LIMIT


def _serialize_context(context: object) -> str:
    data = context.model_dump(mode="json")  # type: ignore[attr-defined]
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


class PydanticSupervisorModel:
    """PydanticAI实现的Supervisor模型端口。

    `goal_system_prompt`/`plan_system_prompt` 为可选静态系统提示，仅在真实模型实调（Canary）
    时注入，用于把候选生成的确定性契约（可引用资源白名单、local_id 约定）告知模型；离线验收
    注入 `FunctionModel` 测试替身时不传（保持裸上下文语义），故默认 `None`。
    """

    def __init__(
        self,
        model: Model,
        goal_system_prompt: str | None = None,
        plan_system_prompt: str | None = None,
    ) -> None:
        self._model = model
        self._goal_system_prompt = goal_system_prompt
        self._plan_system_prompt = plan_system_prompt

    async def generate_goal_understanding(
        self, context: GoalUnderstandingContextV1
    ) -> GoalUnderstandingModelOutputV1:
        agent = Agent(
            self._model,
            name="supervisor-goal-understanding",
            output_type=NativeOutput(GoalUnderstandingModelOutputV1, strict=True),
            system_prompt=(
                self._goal_system_prompt if self._goal_system_prompt is not None else ()
            ),
            tools=[],
            retries=STRUCTURAL_REPAIR_LIMIT,
        )
        agent.instrument = False
        result = await agent.run(_serialize_context(context))
        return result.output

    async def generate_initial_plan(
        self, context: InitialPlanningContextV1
    ) -> InitialPlanModelOutputV1:
        agent = Agent(
            self._model,
            name="supervisor-initial-plan",
            output_type=NativeOutput(InitialPlanModelOutputV1, strict=True),
            system_prompt=(
                self._plan_system_prompt if self._plan_system_prompt is not None else ()
            ),
            tools=[],
            retries=STRUCTURAL_REPAIR_LIMIT,
        )
        agent.instrument = False
        result = await agent.run(_serialize_context(context))
        return result.output


__all__ = ["PydanticSupervisorModel"]
