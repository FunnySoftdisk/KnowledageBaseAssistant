"""登录：口令校验成功后签发访问令牌；不区分账号不存在与口令错误。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from .errors import AuthError, AuthErrorCode
from .gateway import UserAccountGateway
from .password_hasher import PasswordHasher
from .token_service import AccessTokenClaims, AccessTokenService


@dataclass(frozen=True, slots=True)
class LoginResult:
    access_token: str
    expires_in_seconds: int
    user_id: UUID
    organization_id: UUID
    display_name: str
    must_change_password: bool


class AuthenticationService:
    def __init__(
        self,
        tokens: AccessTokenService,
        gateway: UserAccountGateway,
        hasher: PasswordHasher,
    ) -> None:
        self._tokens = tokens
        self._gateway = gateway
        self._hasher = hasher

    async def login(self, username: str, password: str) -> LoginResult:
        normalized = username.strip().casefold()
        user = await self._gateway.by_username(normalized)
        if user is None:
            raise AuthError(AuthErrorCode.AUTH_INVALID_CREDENTIALS)
        if user.status == "DISABLED":
            raise AuthError(AuthErrorCode.AUTH_ACCOUNT_DISABLED)
        now = datetime.now(UTC)
        if user.locked_until is not None and user.locked_until > now:
            raise AuthError(AuthErrorCode.AUTH_ACCOUNT_TEMP_LOCKED)
        if not self._hasher.verify(password, user.password_hash):
            raise AuthError(AuthErrorCode.AUTH_INVALID_CREDENTIALS)
        session_id = uuid4()
        token, claims = self._tokens.issue(user.user_id, session_id, user.credential_version)
        return LoginResult(
            access_token=token,
            expires_in_seconds=self._lifetime_seconds(claims),
            user_id=user.user_id,
            organization_id=user.organization_id,
            display_name=user.display_name,
            must_change_password=(
                user.must_change_password or user.status == "PENDING_FIRST_LOGIN"
            ),
        )

    @staticmethod
    def _lifetime_seconds(claims: AccessTokenClaims) -> int:
        return int((claims.expires_at - claims.issued_at).total_seconds())
