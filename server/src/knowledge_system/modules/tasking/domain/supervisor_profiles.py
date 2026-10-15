"""DEV-05：冻结Supervisor两个Prompt Profile与结构修复预算，登记Profile/Schema/Digest。

每个Profile把名称、输出Schema、结构修复上限绑定为不可变记录，`schema_digest`取自输出候选
DTC的JSON Schema（RFC 8785 JCS+SHA-256），`profile_digest`覆盖全部绑定字段，保证模型调用
始终对接到同一冻结Schema+修复预算。自然语言Prompt模板属Prompt材料，留待后续登记，不在此虚构。
"""

from __future__ import annotations

from typing import Final

from pydantic import TypeAdapter

from .core_types import Sha256Hex, StrictContractV1, canonical_json_sha256
from .supervisor_candidates import (
    GoalUnderstandingModelOutputV1,
    InitialPlanModelOutputV1,
)

GOAL_UNDERSTANDING_PROFILE: Final = "supervisor-goal-understanding-v1"
INITIAL_PLAN_PROFILE: Final = "supervisor-initial-plan-v1"
STRUCTURAL_REPAIR_LIMIT: Final = 2

_GOAL_OUTPUT_SCHEMA_VERSION: Final = "supervisor_goal_understanding_model_output_v1"
_INITIAL_PLAN_OUTPUT_SCHEMA_VERSION: Final = "supervisor_initial_plan_model_output_v1"


class SupervisorProfileV1(StrictContractV1):
    profile_name: str
    output_schema_version: str
    structural_repair_limit: int
    schema_digest: Sha256Hex
    profile_digest: Sha256Hex


def _schema_digest(output_type: type[StrictContractV1]) -> str:
    return canonical_json_sha256(TypeAdapter(output_type).json_schema())


def _freeze_profile(
    profile_name: str, output_schema_version: str, output_type: type[StrictContractV1]
) -> SupervisorProfileV1:
    schema_digest = _schema_digest(output_type)
    profile_digest = canonical_json_sha256(
        {
            "profile_name": profile_name,
            "output_schema_version": output_schema_version,
            "structural_repair_limit": STRUCTURAL_REPAIR_LIMIT,
            "schema_digest": schema_digest,
        }
    )
    return SupervisorProfileV1(
        profile_name=profile_name,
        output_schema_version=output_schema_version,
        structural_repair_limit=STRUCTURAL_REPAIR_LIMIT,
        schema_digest=schema_digest,
        profile_digest=profile_digest,
    )


_SUPERVISOR_PROFILES: Final[dict[str, SupervisorProfileV1]] = {
    GOAL_UNDERSTANDING_PROFILE: _freeze_profile(
        GOAL_UNDERSTANDING_PROFILE,
        _GOAL_OUTPUT_SCHEMA_VERSION,
        GoalUnderstandingModelOutputV1,
    ),
    INITIAL_PLAN_PROFILE: _freeze_profile(
        INITIAL_PLAN_PROFILE,
        _INITIAL_PLAN_OUTPUT_SCHEMA_VERSION,
        InitialPlanModelOutputV1,
    ),
}


def supervisor_profile(profile_name: str) -> SupervisorProfileV1:
    """返回冻结Profile；未登记名称失败关闭。"""

    try:
        return _SUPERVISOR_PROFILES[profile_name]
    except KeyError as error:
        raise ValueError("SUPERVISOR_PROFILE_NOT_REGISTERED") from error


__all__ = [
    "GOAL_UNDERSTANDING_PROFILE",
    "INITIAL_PLAN_PROFILE",
    "STRUCTURAL_REPAIR_LIMIT",
    "SupervisorProfileV1",
    "supervisor_profile",
]
