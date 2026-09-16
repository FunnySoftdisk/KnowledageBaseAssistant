"""IAM读取端口：解耦认证用例与ORM。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class UserAccountSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    user_id: UUID
    organization_id: UUID
    normalized_username: str
    display_name: str
    password_hash: str
    status: str
    must_change_password: bool
    credential_version: int = Field(ge=1)
    locked_until: datetime | None


class UserAccountGateway(Protocol):
    async def by_username(self, normalized_username: str) -> UserAccountSnapshot | None: ...

    async def by_id(self, user_id: UUID) -> UserAccountSnapshot | None: ...
