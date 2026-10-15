"""DEV-06 任务级运行时闭环持久化权威（零 LLM、零 I/O）。

`RuntimeLoopStateV1` 承载 `RuntimeDecisionFactsV1` 中「无既有表可承载」的任务级运行时权威
字段：代数栅栏（`execution_generation`/`cancellation_generation`）、工作版本、阻断/就绪引用、
无进展计数器（`NoProgressStateV1`）与循环级收敛动作（`RuntimeConvergenceActionV1`）。这些字段由
上层 Temporal Activity 在两次 `RuntimeLoopService.advance` 之间推进，并持久化到
`workflow.task_runtime_state`；随后由 PG 只读仓库投影进 `RuntimeDecisionFactsV1`。缺省实例即
「尚未进入运行时闭环」的基线。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator, model_validator

from knowledge_system.modules.tasking.application.runtime_convergence import (
    RuntimeConvergenceActionV1,
)
from knowledge_system.modules.tasking.domain.core_types import StrictContractV1, VersionedRefV1
from knowledge_system.modules.tasking.domain.runtime_decision import (
    RuntimeBlockingIssueV1,
    _normalize_utc,
)
from knowledge_system.modules.tasking.domain.runtime_progress import (
    MAX_REPLAN_PER_TASK,
    NoProgressStateV1,
)


class RuntimeLoopStateV1(StrictContractV1):
    """任务级运行时闭环权威；所有字段均可缺省，缺省即未进入闭环的基线。"""

    work_version: int = Field(default=1, ge=1)
    execution_generation: int = Field(default=0, ge=0)
    cancellation_generation: int = Field(default=0, ge=0)
    next_reconcile_at: datetime | None = None
    blocking_effect_refs: tuple[VersionedRefV1, ...] = Field(default=(), max_length=16)
    blocking_issue: RuntimeBlockingIssueV1 | None = None
    ready_set_ref: VersionedRefV1 | None = None
    no_progress_state: NoProgressStateV1 = Field(default_factory=NoProgressStateV1)
    convergence_action: RuntimeConvergenceActionV1 = RuntimeConvergenceActionV1.CONTINUE_LOOP
    replan_required: bool = False

    @field_validator("next_reconcile_at")
    @classmethod
    def normalize_reconcile(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _normalize_utc(value)

    @model_validator(mode="after")
    def validate_replan_coherence(self) -> RuntimeLoopStateV1:
        # REPLAN 动作必须携带已消费的预算状态（与 `mark_replan_used` 不变式一致）。
        if self.convergence_action is RuntimeConvergenceActionV1.REPLAN:
            if not self.no_progress_state.episode_replan_used:
                raise ValueError("RUNTIME_DECISION_REPLAN_EPISODE_NOT_MARKED")
            if not 1 <= self.no_progress_state.task_replan_count <= MAX_REPLAN_PER_TASK:
                raise ValueError("RUNTIME_DECISION_REPLAN_BUDGET_OUT_OF_RANGE")
        return self


__all__ = ["RuntimeLoopStateV1"]
