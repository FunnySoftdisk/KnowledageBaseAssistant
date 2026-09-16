"""访问令牌：HS256签发与验签；claim仅含sub/sid/credential_version/iat/exp/jti。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from jwt import ExpiredSignatureError, InvalidTokenError

from .errors import AuthError, AuthErrorCode

_HS256 = "HS256"
_MIN_SIGNING_KEY_BYTES = 32


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    subject: UUID
    session_id: UUID
    credential_version: int
    issued_at: datetime
    expires_at: datetime
    token_id: UUID


class AccessTokenService:
    """签发/验签访问令牌；签名密钥由SecretRef在外部解析后注入，不内联。"""

    def __init__(
        self,
        signing_key: bytes,
        *,
        algorithm: str = _HS256,
        token_minutes: int = 15,
    ) -> None:
        if algorithm != _HS256:
            raise ValueError("TOKEN_ALGORITHM_NOT_APPROVED")
        if len(signing_key) < _MIN_SIGNING_KEY_BYTES:
            raise ValueError("TOKEN_SIGNING_KEY_TOO_SHORT")
        if not 5 <= token_minutes <= 60:
            raise ValueError("TOKEN_TTL_OUT_OF_RANGE")
        self._signing_key = signing_key
        self._algorithm = algorithm
        self._token_minutes = token_minutes

    def issue(
        self,
        subject: UUID,
        session_id: UUID,
        credential_version: int,
    ) -> tuple[str, AccessTokenClaims]:
        issued_at = datetime.now(UTC)
        expires_at = issued_at + timedelta(minutes=self._token_minutes)
        claims = AccessTokenClaims(
            subject=subject,
            session_id=session_id,
            credential_version=credential_version,
            issued_at=issued_at,
            expires_at=expires_at,
            token_id=uuid4(),
        )
        token = jwt.encode(
            {
                "sub": str(subject),
                "sid": str(session_id),
                "credential_version": credential_version,
                "jti": str(claims.token_id),
                "iat": int(issued_at.timestamp()),
                "exp": int(expires_at.timestamp()),
            },
            self._signing_key,
            algorithm=self._algorithm,
        )
        return token, claims

    def verify(self, token: str) -> AccessTokenClaims:
        try:
            payload = jwt.decode(token, self._signing_key, algorithms=[self._algorithm])
        except ExpiredSignatureError as error:
            raise AuthError(AuthErrorCode.AUTH_TOKEN_EXPIRED, retryable=True) from error
        except InvalidTokenError as error:
            raise AuthError(AuthErrorCode.AUTH_TOKEN_INVALID) from error

        try:
            subject = UUID(payload["sub"])
            session_id = UUID(payload["sid"])
            token_id = UUID(payload["jti"])
            credential_version = int(payload["credential_version"])
            issued_at = datetime.fromtimestamp(int(payload["iat"]), tz=UTC)
            expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
        except (KeyError, TypeError, ValueError) as error:
            raise AuthError(AuthErrorCode.AUTH_TOKEN_INVALID) from error
        if credential_version < 1:
            raise AuthError(AuthErrorCode.AUTH_TOKEN_INVALID)
        return AccessTokenClaims(
            subject=subject,
            session_id=session_id,
            credential_version=credential_version,
            issued_at=issued_at,
            expires_at=expires_at,
            token_id=token_id,
        )
