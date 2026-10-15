"""DEV-05：确定性Goal Gate。

模型候选装配为完整`GoalUnderstandingV1`后，由服务端确定性判定`READY_TO_PLAN`还是
`REQUEST_USER_INPUT`：存在模型自报的阻断Issue即请求用户输入（携带validated_issue_ids与
reason_codes），否则进入规划并接受全部安全假设（accepted_assumption_ids）。语义引用校验
（`validate_goal_relations`）由调用方在Gate之前执行，非法引用属稳定拒绝而非Gate结果。
"""

from __future__ import annotations

from typing import Final

from .core_types import canonical_json_sha256
from .goal_contracts import GoalGateDecisionV1, GoalUnderstandingV1
from .supervisor_candidates import goal_understanding_id

GOAL_GATE_PROFILE: Final = "supervisor-goal-gate-v1"
GOAL_GATE_PROFILE_DIGEST: Final = canonical_json_sha256(
    {
        "gate_profile": GOAL_GATE_PROFILE,
        "outcomes": ["READY_TO_PLAN", "REQUEST_USER_INPUT"],
        "decision": "blocking_issues_nonempty_implies_request_user_input",
    }
)


def decide_goal_gate(goal: GoalUnderstandingV1) -> GoalGateDecisionV1:
    """只按模型自报阻断Issue判定；不理解内容、不授权、不I/O。"""

    understanding_digest = canonical_json_sha256(goal.model_dump(mode="json"))
    understanding_id = goal_understanding_id(understanding_digest)

    if goal.blocking_issues:
        return GoalGateDecisionV1(
            understanding_id=understanding_id,
            understanding_digest=understanding_digest,
            outcome="REQUEST_USER_INPUT",
            validated_issue_ids=tuple(issue.local_id for issue in goal.blocking_issues),
            accepted_assumption_ids=(),
            reason_codes=tuple(issue.reason for issue in goal.blocking_issues),
            gate_profile_digest=GOAL_GATE_PROFILE_DIGEST,
        )
    return GoalGateDecisionV1(
        understanding_id=understanding_id,
        understanding_digest=understanding_digest,
        outcome="READY_TO_PLAN",
        validated_issue_ids=(),
        accepted_assumption_ids=tuple(assumption.local_id for assumption in goal.safe_assumptions),
        reason_codes=(),
        gate_profile_digest=GOAL_GATE_PROFILE_DIGEST,
    )


__all__ = [
    "GOAL_GATE_PROFILE",
    "GOAL_GATE_PROFILE_DIGEST",
    "decide_goal_gate",
]
