"""API运行时配置；签名密钥以SecretRef注入，不内联明文。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SettingsError(RuntimeError):
    """配置缺失或密钥不可读。"""


class SecretRef(BaseModel):
    """密钥引用；resolve()在启动时读取实际字节，Settings本身不含明文。"""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    kind: Literal["env", "file"]
    reference: str = Field(min_length=1, max_length=512)

    def resolve(self) -> bytes:
        if self.kind == "env":
            value = os.environ.get(self.reference)
            if value is None:
                raise SettingsError(f"SECRET_ENV_NOT_SET:{self.reference}")
            return value.encode("utf-8")
        path = Path(self.reference)
        try:
            return path.read_bytes()
        except OSError as error:
            raise SettingsError(f"SECRET_FILE_UNREADABLE:{self.reference}") from error


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    database_url: str
    jwt_signing_secret_ref: SecretRef
    idempotency_hmac_secret_ref: SecretRef
    access_token_minutes: int = Field(default=15, ge=5, le=60)
    task_deadline_seconds: int = Field(default=600, ge=1)
    task_locale: str = Field(default="zh-CN", min_length=1, max_length=35)
    task_timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)

    def jwt_signing_secret(self) -> bytes:
        return self.jwt_signing_secret_ref.resolve()

    def idempotency_hmac_secret(self) -> bytes:
        return self.idempotency_hmac_secret_ref.resolve()

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ

        def required(name: str) -> str:
            value = env.get(name)
            if value is None:
                raise SettingsError(f"ENV_NOT_SET:{name}")
            return value

        return cls(
            database_url=required("KNOWLEDGE_DATABASE_URL"),
            jwt_signing_secret_ref=SecretRef(
                kind="file", reference=required("KNOWLEDGE_JWT_SIGNING_SECRET_FILE")
            ),
            idempotency_hmac_secret_ref=SecretRef(
                kind="file", reference=required("KNOWLEDGE_IDEMPOTENCY_HMAC_SECRET_FILE")
            ),
            access_token_minutes=int(env.get("KNOWLEDGE_ACCESS_TOKEN_MINUTES", "15")),
            task_deadline_seconds=int(env.get("KNOWLEDGE_TASK_DEADLINE_SECONDS", "600")),
            task_locale=env.get("KNOWLEDGE_TASK_LOCALE", "zh-CN"),
            task_timezone=env.get("KNOWLEDGE_TASK_TIMEZONE", "Asia/Shanghai"),
        )
