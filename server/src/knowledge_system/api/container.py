"""接口层组合根：把静态配置与每请求Session装配为应用服务。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_system.infrastructure.persistence import (
    IdentityRepository,
    SqlAlchemyUnitOfWork,
    TaskCreationTransactionService,
    TaskReadRepository,
)
from knowledge_system.modules.iam.public import (
    AccessTokenService,
    Argon2PasswordHasher,
    AuthenticationService,
    SubjectResolver,
)
from knowledge_system.modules.tasking.public import TaskCreationService, TaskQueryService

from .settings import Settings


@dataclass(frozen=True, slots=True)
class AppRuntime:
    """只读装配；每请求用request.state.runtime取用。"""

    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    token_service: AccessTokenService
    idempotency_hmac_secret: bytes

    def subject_resolver(self, session: AsyncSession) -> SubjectResolver:
        return SubjectResolver(self.token_service, IdentityRepository(session))

    def authentication_service(self, session: AsyncSession) -> AuthenticationService:
        return AuthenticationService(
            self.token_service,
            IdentityRepository(session),
            Argon2PasswordHasher(),
        )

    def task_creation_service(self, session: AsyncSession) -> TaskCreationService:
        return TaskCreationService(
            TaskCreationTransactionService(
                lambda: SqlAlchemyUnitOfWork(self.session_factory)
            ),
            TaskReadRepository(session),
            self.idempotency_hmac_secret,
            locale=self.settings.task_locale,
            timezone=self.settings.task_timezone,
            deadline_seconds=self.settings.task_deadline_seconds,
        )

    def task_query_service(self, session: AsyncSession) -> TaskQueryService:
        return TaskQueryService(TaskReadRepository(session))
