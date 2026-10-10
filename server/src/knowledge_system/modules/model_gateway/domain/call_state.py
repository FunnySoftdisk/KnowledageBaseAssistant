"""ModelCall状态机纯规则。

依据模型网关模块设计§12：正常链`ACCEPTED -> SENDING -> STREAMING -> COMPLETED`，异常终态
`FAILED/CANCELLED/UNKNOWN`。Gateway在消费事务提交时写`ACCEPTED`、Provider调用前写`SENDING`；
网络结果未知不得误标失败。终态一旦写入即不可再转移，重复`model_call_id`只能返回既有结果或继续
同一受控调用，不能新建第二条计费记录。
"""

from enum import StrEnum


class ModelCallState(StrEnum):
    ACCEPTED = "ACCEPTED"
    SENDING = "SENDING"
    STREAMING = "STREAMING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


TERMINAL_STATES = frozenset(
    {
        ModelCallState.COMPLETED,
        ModelCallState.FAILED,
        ModelCallState.CANCELLED,
        ModelCallState.UNKNOWN,
    }
)

# 合法转移表：ACCEPTED只进SENDING；SENDING/STREAMING可正向推进或落入异常终态。
_TRANSITIONS: dict[ModelCallState, frozenset[ModelCallState]] = {
    ModelCallState.ACCEPTED: frozenset({ModelCallState.SENDING}),
    ModelCallState.SENDING: frozenset(
        {
            ModelCallState.STREAMING,
            ModelCallState.COMPLETED,
            ModelCallState.FAILED,
            ModelCallState.CANCELLED,
            ModelCallState.UNKNOWN,
        }
    ),
    ModelCallState.STREAMING: frozenset(
        {
            ModelCallState.COMPLETED,
            ModelCallState.FAILED,
            ModelCallState.CANCELLED,
            ModelCallState.UNKNOWN,
        }
    ),
}


class ModelCallStateViolation(ValueError):
    """稳定code可公开；异常中不保存输入正文、Secret或Provider原包。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def is_terminal(state: ModelCallState) -> bool:
    """终态（COMPLETED/FAILED/CANCELLED/UNKNOWN）不可再转移。"""
    return state in TERMINAL_STATES


def can_transition(current: ModelCallState, target: ModelCallState) -> bool:
    """判断一次状态转移是否落在合法转移表内。"""
    if current in TERMINAL_STATES:
        return False
    return target in _TRANSITIONS[current]


def require_transition(current: ModelCallState, target: ModelCallState) -> None:
    """校验转移合法，否则抛稳定`MODEL_CALL_STATE_TRANSITION_INVALID`。"""
    if not can_transition(current, target):
        raise ModelCallStateViolation("MODEL_CALL_STATE_TRANSITION_INVALID")


__all__ = [
    "ModelCallState",
    "ModelCallStateViolation",
    "TERMINAL_STATES",
    "can_transition",
    "is_terminal",
    "require_transition",
]
