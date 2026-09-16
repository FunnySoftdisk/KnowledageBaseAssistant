"""IAM认证错误；code为《错误码与异常处理规范》中的稳定机器契约。"""

from __future__ import annotations

from enum import StrEnum


class AuthErrorCode(StrEnum):
    AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"
    AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
    AUTH_TOKEN_INVALID = "AUTH_TOKEN_INVALID"
    AUTH_SESSION_REVOKED = "AUTH_SESSION_REVOKED"
    AUTH_PASSWORD_CHANGE_REQUIRED = "AUTH_PASSWORD_CHANGE_REQUIRED"
    AUTH_ACCOUNT_DISABLED = "AUTH_ACCOUNT_DISABLED"
    AUTH_ACCOUNT_TEMP_LOCKED = "AUTH_ACCOUNT_TEMP_LOCKED"


class AuthError(RuntimeError):
    """认证失败；HTTP状态与retryable由API层按code映射。"""

    def __init__(self, code: AuthErrorCode, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code.value)
