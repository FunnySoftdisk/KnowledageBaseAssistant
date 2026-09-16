"""认证路由：登录与当前主体查询。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from knowledge_system.modules.iam.public import AuthenticationService, Subject

from ..dependencies import get_authentication_service, get_subject
from ..schemas import LoginRequest, LoginResponse, MeResponse

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    service: Annotated[AuthenticationService, Depends(get_authentication_service)],
) -> LoginResponse:
    result = await service.login(body.username, body.password)
    return LoginResponse(
        access_token=result.access_token,
        token_type="Bearer",
        expires_in_seconds=result.expires_in_seconds,
        user_id=result.user_id,
        organization_id=result.organization_id,
        display_name=result.display_name,
        must_change_password=result.must_change_password,
    )


@router.get("/me", response_model=MeResponse)
async def me(subject: Annotated[Subject, Depends(get_subject)]) -> MeResponse:
    return MeResponse(
        user_id=subject.user_id,
        organization_id=subject.organization_id,
        credential_version=subject.credential_version,
    )
