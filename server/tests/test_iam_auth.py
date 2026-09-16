"""DEV-02A IAM最小认证链：口令散列、访问令牌、登录与Subject解析。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt as pyjwt

from knowledge_system.modules.iam.application.authentication import (
    AuthenticationService,
)
from knowledge_system.modules.iam.application.errors import AuthError, AuthErrorCode
from knowledge_system.modules.iam.application.gateway import UserAccountSnapshot
from knowledge_system.modules.iam.application.password_hasher import Argon2PasswordHasher
from knowledge_system.modules.iam.application.subject_resolver import SubjectResolver
from knowledge_system.modules.iam.application.token_service import AccessTokenService

SIGNING_KEY = b"s" * 32


def make_user(**overrides: object) -> UserAccountSnapshot:
    base = dict(
        user_id=uuid4(),
        organization_id=uuid4(),
        normalized_username="alice",
        display_name="Alice",
        password_hash="ignored",
        status="ACTIVE",
        must_change_password=False,
        credential_version=1,
        locked_until=None,
    )
    base.update(overrides)
    return UserAccountSnapshot(**base)


class FakeUserAccountGateway:
    def __init__(self, users: list[UserAccountSnapshot]) -> None:
        self.by_id_map = {user.user_id: user for user in users}
        self.by_username_map = {user.normalized_username: user for user in users}

    async def by_id(self, user_id: UUID) -> UserAccountSnapshot | None:
        return self.by_id_map.get(user_id)

    async def by_username(self, normalized_username: str) -> UserAccountSnapshot | None:
        return self.by_username_map.get(normalized_username)


class Argon2PasswordHasherTests(unittest.TestCase):
    def test_hash_verify_and_needs_rehash(self) -> None:
        hasher = Argon2PasswordHasher()
        encoded = hasher.hash("correct horse battery staple")
        self.assertTrue(hasher.verify("correct horse battery staple", encoded))
        self.assertFalse(hasher.verify("wrong", encoded))
        self.assertFalse(hasher.needs_rehash(encoded))

    def test_needs_rehash_on_garbage(self) -> None:
        self.assertTrue(Argon2PasswordHasher().needs_rehash("not-a-hash"))


class AccessTokenServiceTests(unittest.TestCase):
    def test_round_trip_preserves_claims(self) -> None:
        service = AccessTokenService(SIGNING_KEY)
        subject = uuid4()
        session = uuid4()
        token, claims = service.issue(subject, session, 3)
        decoded = service.verify(token)
        self.assertEqual(decoded.subject, subject)
        self.assertEqual(decoded.session_id, session)
        self.assertEqual(decoded.credential_version, 3)
        self.assertEqual(decoded.token_id, claims.token_id)

    def test_tampered_token_is_invalid(self) -> None:
        service = AccessTokenService(SIGNING_KEY)
        token, _ = service.issue(uuid4(), uuid4(), 1)
        with self.assertRaises(AuthError) as caught:
            service.verify(token + "tampered")
        self.assertEqual(caught.exception.code, AuthErrorCode.AUTH_TOKEN_INVALID)

    def test_expired_token_is_expired(self) -> None:
        service = AccessTokenService(SIGNING_KEY)
        now = int(datetime.now(UTC).timestamp())
        expired = pyjwt.encode(
            {
                "sub": str(uuid4()),
                "sid": str(uuid4()),
                "credential_version": 1,
                "jti": str(uuid4()),
                "iat": now - 100,
                "exp": now - 50,
            },
            SIGNING_KEY,
            algorithm="HS256",
        )
        with self.assertRaises(AuthError) as caught:
            service.verify(expired)
        self.assertEqual(caught.exception.code, AuthErrorCode.AUTH_TOKEN_EXPIRED)

    def test_short_key_and_bad_algorithm_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "TOKEN_SIGNING_KEY_TOO_SHORT"):
            AccessTokenService(b"short")
        with self.assertRaisesRegex(ValueError, "TOKEN_ALGORITHM_NOT_APPROVED"):
            AccessTokenService(SIGNING_KEY, algorithm="RS256")


class SubjectResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_user_resolves_to_subject(self) -> None:
        user = make_user()
        tokens = AccessTokenService(SIGNING_KEY)
        token, _ = tokens.issue(user.user_id, uuid4(), user.credential_version)
        resolver = SubjectResolver(tokens, FakeUserAccountGateway([user]))
        subject = await resolver.resolve(token)
        self.assertEqual(subject.user_id, user.user_id)
        self.assertEqual(subject.organization_id, user.organization_id)

    async def test_credential_version_mismatch_is_invalid(self) -> None:
        user = make_user(credential_version=2)
        tokens = AccessTokenService(SIGNING_KEY)
        token, _ = tokens.issue(user.user_id, uuid4(), 1)
        resolver = SubjectResolver(tokens, FakeUserAccountGateway([user]))
        with self.assertRaises(AuthError) as caught:
            await resolver.resolve(token)
        self.assertEqual(caught.exception.code, AuthErrorCode.AUTH_TOKEN_INVALID)

    async def test_disabled_and_locked_and_must_change_are_blocked(self) -> None:
        cases = [
            (make_user(status="DISABLED"), AuthErrorCode.AUTH_ACCOUNT_DISABLED),
            (
                make_user(locked_until=datetime.now(UTC) + timedelta(minutes=1)),
                AuthErrorCode.AUTH_ACCOUNT_TEMP_LOCKED,
            ),
            (make_user(must_change_password=True), AuthErrorCode.AUTH_PASSWORD_CHANGE_REQUIRED),
        ]
        for user, expected in cases:
            with self.subTest(expected=expected):
                tokens = AccessTokenService(SIGNING_KEY)
                token, _ = tokens.issue(user.user_id, uuid4(), user.credential_version)
                resolver = SubjectResolver(tokens, FakeUserAccountGateway([user]))
                with self.assertRaises(AuthError) as caught:
                    await resolver.resolve(token)
                self.assertEqual(caught.exception.code, expected)

    async def test_unknown_user_is_invalid(self) -> None:
        tokens = AccessTokenService(SIGNING_KEY)
        token, _ = tokens.issue(uuid4(), uuid4(), 1)
        resolver = SubjectResolver(tokens, FakeUserAccountGateway([]))
        with self.assertRaises(AuthError) as caught:
            await resolver.resolve(token)
        self.assertEqual(caught.exception.code, AuthErrorCode.AUTH_TOKEN_INVALID)


class AuthenticationServiceTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, user: UserAccountSnapshot) -> AuthenticationService:
        hasher = Argon2PasswordHasher()
        password_hash = hasher.hash("secret-password")
        stored = user.model_copy(update={"password_hash": password_hash})
        return AuthenticationService(
            AccessTokenService(SIGNING_KEY),
            FakeUserAccountGateway([stored]),
            hasher,
        )

    async def test_login_returns_token_and_summary(self) -> None:
        user = make_user()
        result = await self._service(user).login("ALICE", "secret-password")
        self.assertTrue(result.access_token)
        self.assertEqual(result.user_id, user.user_id)
        self.assertEqual(result.organization_id, user.organization_id)
        self.assertFalse(result.must_change_password)

    async def test_wrong_password_and_unknown_user_are_indistinguishable(self) -> None:
        user = make_user()
        for username, password in [("alice", "wrong"), ("nobody", "secret-password")]:
            with self.subTest(username=username):
                with self.assertRaises(AuthError) as caught:
                    await self._service(user).login(username, password)
                self.assertEqual(
                    caught.exception.code, AuthErrorCode.AUTH_INVALID_CREDENTIALS
                )

    async def test_disabled_account_is_blocked_before_verify(self) -> None:
        user = make_user(status="DISABLED")
        with self.assertRaises(AuthError) as caught:
            await self._service(user).login("alice", "secret-password")
        self.assertEqual(caught.exception.code, AuthErrorCode.AUTH_ACCOUNT_DISABLED)


if __name__ == "__main__":
    unittest.main()
