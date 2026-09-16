"""FastAPI应用工厂：装配引擎、组合根、请求上下文中间件与统一错误信封。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, Response

from knowledge_system.infrastructure.persistence import (
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
)
from knowledge_system.modules.iam.public import AccessTokenService

from .container import AppRuntime
from .errors import register_exception_handlers
from .routers import auth as auth_router
from .routers import tasks as tasks_router
from .settings import Settings


def create_app(settings: Settings) -> FastAPI:
    engine = build_async_engine(DatabaseEngineSettings(url=settings.database_url))
    session_factory = build_session_factory(engine)
    token_service = AccessTokenService(
        settings.jwt_signing_secret(),
        token_minutes=settings.access_token_minutes,
    )
    runtime = AppRuntime(
        settings=settings,
        session_factory=session_factory,
        token_service=token_service,
        idempotency_hmac_secret=settings.idempotency_hmac_secret(),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        yield
        await engine.dispose()

    app = FastAPI(
        title="高校科研团队私有化多 Agent 知识库 API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.runtime = runtime

    @app.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.request_id = uuid4().hex
        request.state.trace_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    register_exception_handlers(app)
    app.include_router(auth_router.router)
    app.include_router(tasks_router.router)
    return app
