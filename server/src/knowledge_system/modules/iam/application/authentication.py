"""登录：口令校验成功后签发访问令牌；不区分账号不存在与口令错误。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from knowledge_system.modules.audit.public import AuditEventDraft, AuditResult, AuditWriter

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
        *,
        audit_writer: AuditWriter | None = None,
    ) -> None:
        self._tokens = tokens
        self._gateway = gateway
        self._hasher = hasher
        self._audit_writer = audit_writer

    async def login(
        self,
        username: str,
        password: str,
        *,
        trace_id: str | None = None,
    ) -> LoginResult:
        normalized = username.strip().casefold()
        user = await self._gateway.by_username(normalized)
        now = datetime.now(UTC)
        audit_trace_id = trace_id or uuid4().hex
        if user is None:
            await self._append_login(
                None,
                AuditResult.FAILURE,
                AuthErrorCode.AUTH_INVALID_CREDENTIALS,
                audit_trace_id,
                now,
            )
            raise AuthError(AuthErrorCode.AUTH_INVALID_CREDENTIALS)
        if user.status == "DISABLED":
            await self._append_login(
                user.user_id,
                AuditResult.FAILURE,
                AuthErrorCode.AUTH_ACCOUNT_DISABLED,
                audit_trace_id,
                now,
            )
            raise AuthError(AuthErrorCode.AUTH_ACCOUNT_DISABLED)
        if user.locked_until is not None and user.locked_until > now:
            await self._append_login(
                user.user_id,
                AuditResult.FAILURE,
                AuthErrorCode.AUTH_ACCOUNT_TEMP_LOCKED,
                audit_trace_id,
                now,
            )
            raise AuthError(AuthErrorCode.AUTH_ACCOUNT_TEMP_LOCKED)
        if not self._hasher.verify(password, user.password_hash):
            await self._append_login(
                user.user_id,
                AuditResult.FAILURE,
                AuthErrorCode.AUTH_INVALID_CREDENTIALS,
                audit_trace_id,
                now,
            )
            raise AuthError(AuthErrorCode.AUTH_INVALID_CREDENTIALS)
        session_id = uuid4()
        token, claims = self._tokens.issue(user.user_id, session_id, user.credential_version)
        await self._append_login(
            user.user_id,
            AuditResult.SUCCESS,
            None,
            audit_trace_id,
            now,
            session_id=session_id,
        )
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

    async def _append_login(
        self,
        actor_id: UUID | None,
        result: AuditResult,
        reason_code: AuthErrorCode | None,
        trace_id: str,
        now: datetime,
        *,
        session_id: UUID | None = None,
    ) -> None:
        if self._audit_writer is None:
            return
        await self._audit_writer.append_committed(
            AuditEventDraft(
                event_id=uuid4(),
                occurred_at=now,
                actor_id=actor_id,
                actor_role_snapshot=(),
                session_id=session_id,
                source_ip=None,
                device_id=None,
                action="LOGIN",
                resource_type="USER_ACCOUNT",
                resource_id=None if actor_id is None else str(actor_id),
                result=result.value,
                reason_code=None if reason_code is None else reason_code.value,
                before_digest=None,
                after_digest=None,
                details_json=None,
                trace_id=trace_id,
            )
        )

    @staticmethod
    def _lifetime_seconds(claims: AccessTokenClaims) -> int:
        return int((claims.expires_at - claims.issued_at).total_seconds())
