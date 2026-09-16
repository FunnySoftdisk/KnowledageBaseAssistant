"""CORE-17：已认证Subject；由Token验签后按PG权威状态重校验得到。"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Subject(BaseModel):
    """服务端重校验后的认证主体；角色与授权快照仍在后续里程碑闭合。"""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    user_id: UUID
    session_id: UUID
    organization_id: UUID
    credential_version: int = Field(ge=1)
