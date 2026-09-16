"""IAM对外公开接口；API层与测试只引用本模块。"""

from .application.authentication import AuthenticationService, LoginResult
from .application.errors import AuthError, AuthErrorCode
from .application.gateway import UserAccountGateway, UserAccountSnapshot
from .application.password_hasher import Argon2PasswordHasher, PasswordHasher
from .application.subject_resolver import SubjectResolver
from .application.token_service import AccessTokenClaims, AccessTokenService
from .domain.subject import Subject

__all__ = [
    "AccessTokenClaims",
    "AccessTokenService",
    "Argon2PasswordHasher",
    "AuthError",
    "AuthErrorCode",
    "AuthenticationService",
    "LoginResult",
    "PasswordHasher",
    "Subject",
    "SubjectResolver",
    "UserAccountGateway",
    "UserAccountSnapshot",
]
