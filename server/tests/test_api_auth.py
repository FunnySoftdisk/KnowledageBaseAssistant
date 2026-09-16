"""DEV-02C API认证路由：登录与当前主体，覆盖统一错误信封。"""

from __future__ import annotations

import os
import unittest
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from knowledge_system.api.app import create_app
from knowledge_system.api.dependencies import get_authentication_service, get_subject
from knowledge_system.api.settings import SecretRef, Settings
from knowledge_system.modules.iam.public import (
    AccessTokenService,
    Argon2PasswordHasher,
    AuthenticationService,
    AuthError,
    AuthErrorCode,
    Subject,
    UserAccountSnapshot,
)

os.environ.setdefault("TEST_JWT_SIGNING_SECRET", "j" * 64)
os.environ.setdefault("TEST_IDEMPOTENCY_HMAC_SECRET", "h" * 64)


def make_settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://u:p@localhost:5432/db",
        jwt_signing_secret_ref=SecretRef(kind="env", reference="TEST_JWT_SIGNING_SECRET"),
        idempotency_hmac_secret_ref=SecretRef(
            kind="env", reference="TEST_IDEMPOTENCY_HMAC_SECRET"
        ),
    )


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


def make_subject(**overrides: object) -> Subject:
    base = dict(
        user_id=uuid4(),
        session_id=uuid4(),
        organization_id=uuid4(),
        credential_version=1,
    )
    base.update(overrides)
    return Subject(**base)


class _FakeUserAccountGateway:
    def __init__(self, users: list[UserAccountSnapshot]) -> None:
        self._by_id = {u.user_id: u for u in users}
        self._by_username = {u.normalized_username: u for u in users}

    async def by_id(self, user_id: UUID) -> UserAccountSnapshot | None:
        return self._by_id.get(user_id)

    async def by_username(self, normalized_username: str) -> UserAccountSnapshot | None:
        return self._by_username.get(normalized_username)


def _auth_service(user: UserAccountSnapshot) -> AuthenticationService:
    hasher = Argon2PasswordHasher()
    stored = user.model_copy(update={"password_hash": hasher.hash("secret-password")})
    return AuthenticationService(
        AccessTokenService(b"s" * 32),
        _FakeUserAccountGateway([stored]),
        hasher,
    )


class LoginRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(make_settings())
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()

    def test_login_returns_token_and_summary(self) -> None:
        user = make_user()
        self.app.dependency_overrides[get_authentication_service] = lambda: _auth_service(user)
        resp = self.client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "secret-password"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["token_type"], "Bearer")
        self.assertEqual(body["user_id"], str(user.user_id))
        self.assertEqual(body["organization_id"], str(user.organization_id))
        self.assertEqual(body["display_name"], "Alice")
        self.assertTrue(body["access_token"])
        self.assertTrue(body["expires_in_seconds"] > 0)

    def test_login_invalid_credentials_returns_401_envelope(self) -> None:
        user = make_user()
        self.app.dependency_overrides[get_authentication_service] = lambda: _auth_service(user)
        resp = self.client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "wrong"},
        )
        self.assertEqual(resp.status_code, 401)
        body = resp.json()
        self.assertEqual(body["code"], "AUTH_INVALID_CREDENTIALS")
        self.assertIn("request_id", body)
        self.assertIn("trace_id", body)
        self.assertIn("server_time", body)
        self.assertEqual(resp.headers["X-Request-Id"], body["request_id"])

    def test_login_body_validation_returns_field_errors(self) -> None:
        resp = self.client.post("/api/v1/auth/login", json={"username": ""})
        self.assertEqual(resp.status_code, 422)
        body = resp.json()
        self.assertEqual(body["code"], "REQUEST_VALIDATION_FAILED")
        self.assertTrue(body["field_errors"])


class MeRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(make_settings())
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()

    def test_me_returns_resolved_subject(self) -> None:
        subject = make_subject()
        self.app.dependency_overrides[get_subject] = lambda: subject
        resp = self.client.get("/api/v1/auth/me")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["user_id"], str(subject.user_id))
        self.assertEqual(body["organization_id"], str(subject.organization_id))
        self.assertEqual(body["credential_version"], subject.credential_version)

    def test_me_missing_token_returns_401_envelope(self) -> None:
        def deny() -> Subject:
            raise AuthError(AuthErrorCode.AUTH_TOKEN_INVALID)

        self.app.dependency_overrides[get_subject] = deny
        resp = self.client.get("/api/v1/auth/me")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], "AUTH_TOKEN_INVALID")


if __name__ == "__main__":
    unittest.main()
