"""REST请求/响应模型；认证与任务入口的公开契约。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# 公开REST模型不需要严格frozen；仅约束字段形状。


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class LoginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    access_token: str
    token_type: str
    expires_in_seconds: int
    user_id: UUID
    organization_id: UUID
    display_name: str
    must_change_password: bool


class MeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: UUID
    organization_id: UUID
    credential_version: int


class TaskCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    input_id: UUID
    status: str
    deadline_at: datetime
    active_plan_version: int | None
