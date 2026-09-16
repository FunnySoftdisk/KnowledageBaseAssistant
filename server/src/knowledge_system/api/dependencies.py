"""FastAPI依赖：每请求Session、认证Subject与应用服务装配。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_system.modules.iam.public import (
    AuthenticationService,
    AuthError,
    AuthErrorCode,
    Subject,
)
from knowledge_system.modules.tasking.public import TaskCreationService, TaskQueryService

from .container import AppRuntime


def get_runtime(request: Request) -> AppRuntime:
    runtime: AppRuntime = request.app.state.runtime
    return runtime


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    runtime: AppRuntime = request.app.state.runtime
    async with runtime.session_factory() as session:
        yield session


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization")
    if authorization is None:
        raise AuthError(AuthErrorCode.AUTH_TOKEN_INVALID)
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError(AuthErrorCode.AUTH_TOKEN_INVALID)
    return token.strip()


async def get_subject(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Subject:
    runtime: AppRuntime = request.app.state.runtime
    return await runtime.subject_resolver(session).resolve(_bearer_token(request))


def get_authentication_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthenticationService:
    runtime: AppRuntime = request.app.state.runtime
    return runtime.authentication_service(session)


def get_task_creation_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TaskCreationService:
    runtime: AppRuntime = request.app.state.runtime
    return runtime.task_creation_service(session)


def get_task_query_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TaskQueryService:
    runtime: AppRuntime = request.app.state.runtime
    return runtime.task_query_service(session)
