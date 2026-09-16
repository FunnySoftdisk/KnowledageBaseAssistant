"""Subject解析：验签访问令牌后按PG权威账号状态重校验。"""

from __future__ import annotations

from datetime import UTC, datetime

from ..domain.subject import Subject
from .errors import AuthError, AuthErrorCode
from .gateway import UserAccountGateway
from .token_service import AccessTokenService


class SubjectResolver:
    def __init__(self, tokens: AccessTokenService, gateway: UserAccountGateway) -> None:
        self._tokens = tokens
        self._gateway = gateway

    async def resolve(self, raw_token: str) -> Subject:
        claims = self._tokens.verify(raw_token)
        user = await self._gateway.by_id(claims.subject)
        if user is None:
            raise AuthError(AuthErrorCode.AUTH_TOKEN_INVALID)
        if user.status == "DISABLED":
            raise AuthError(AuthErrorCode.AUTH_ACCOUNT_DISABLED)
        if user.status == "PENDING_FIRST_LOGIN" or user.must_change_password:
            raise AuthError(AuthErrorCode.AUTH_PASSWORD_CHANGE_REQUIRED)
        now = datetime.now(UTC)
        if user.locked_until is not None and user.locked_until > now:
            raise AuthError(AuthErrorCode.AUTH_ACCOUNT_TEMP_LOCKED)
        if user.credential_version != claims.credential_version:
            raise AuthError(AuthErrorCode.AUTH_TOKEN_INVALID)
        return Subject(
            user_id=user.user_id,
            session_id=claims.session_id,
            organization_id=user.organization_id,
            credential_version=user.credential_version,
        )
