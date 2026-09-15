"""ADR-0001的本批一般开发试跑规则，不是正式Agent资格判定。"""

from enum import StrEnum


class DevelopmentPolicyViolation(ValueError):
    """稳定code可公开；异常中不保存输入正文、Secret或Provider原包。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AccountingMode(StrEnum):
    EXACT = "EXACT"
    ESTIMATED_DEV = "ESTIMATED_DEV"


DEVELOPMENT_ALIASES = frozenset({"planning_strong", "synthesis_action", "synthesis_strong"})
DEVELOPMENT_PROFILE = "synthesis-action-user-facts-r0-v1"
MAX_INPUT_BYTES = 8192


def validate_trial_input(*, app_env: str, data_scope: str, text: str) -> None:
    """只验证环境、声明范围及输入大小；不证明数据真实非敏感或授权已通过。"""
    if app_env != "development":
        raise DevelopmentPolicyViolation("DEV_TRIAL_ENVIRONMENT_FORBIDDEN")
    if data_scope != "PUBLIC_DEVELOPMENT":
        raise DevelopmentPolicyViolation("DEV_TRIAL_DATA_SCOPE_FORBIDDEN")
    if not text.strip():
        raise DevelopmentPolicyViolation("DEV_TRIAL_INPUT_EMPTY")
    try:
        byte_count = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise DevelopmentPolicyViolation("DEV_TRIAL_INPUT_ENCODING_INVALID") from exc
    if byte_count > MAX_INPUT_BYTES:
        raise DevelopmentPolicyViolation("DEV_TRIAL_INPUT_TOO_LARGE")


def validate_estimated_budget(
    *,
    app_env: str,
    capability_alias: str,
    development_manifest_authorized: bool,
    requires_exact_tokens: bool,
    estimated_input_tokens: int,
    estimated_window_tokens: int,
    stage_input_limit: int,
    max_output_tokens: int,
) -> None:
    """只验证计数边界；必须由上层另验Manifest/Context/授权/安全/预算预留。"""
    if app_env != "development" or capability_alias not in DEVELOPMENT_ALIASES:
        raise DevelopmentPolicyViolation("MODEL_ESTIMATED_ACCOUNTING_FORBIDDEN")
    if development_manifest_authorized is not True or requires_exact_tokens is not False:
        raise DevelopmentPolicyViolation("MODEL_ESTIMATED_ACCOUNTING_FORBIDDEN")
    counts = (
        estimated_input_tokens,
        estimated_window_tokens,
        stage_input_limit,
        max_output_tokens,
    )
    if any(type(value) is not int for value in counts):
        raise DevelopmentPolicyViolation("MODEL_TOKEN_BUDGET_INVALID")
    if estimated_input_tokens < 0 or any(value <= 0 for value in counts[1:]):
        raise DevelopmentPolicyViolation("MODEL_TOKEN_BUDGET_INVALID")
    if (
        estimated_input_tokens * 5 > estimated_window_tokens * 4
        or estimated_input_tokens > stage_input_limit
        or estimated_input_tokens + max_output_tokens > estimated_window_tokens
    ):
        raise DevelopmentPolicyViolation("MODEL_CONTEXT_EXCEEDED")
