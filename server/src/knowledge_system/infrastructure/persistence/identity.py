"""IAM账号读取SQLAlchemy适配器；实现UserAccountGateway端口。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_system.modules.iam.application.gateway import UserAccountSnapshot

from .foundation_models import UserAccountRecord


class IdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def by_username(self, normalized_username: str) -> UserAccountSnapshot | None:
        statement: Select[tuple[UserAccountRecord]] = select(UserAccountRecord).where(
            UserAccountRecord.normalized_username == normalized_username
        )
        record = (await self._session.scalars(statement)).first()
        return None if record is None else self._snapshot(record)

    async def by_id(self, user_id: UUID) -> UserAccountSnapshot | None:
        statement: Select[tuple[UserAccountRecord]] = select(UserAccountRecord).where(
            UserAccountRecord.id == user_id
        )
        record = (await self._session.scalars(statement)).first()
        return None if record is None else self._snapshot(record)

    @staticmethod
    def _snapshot(record: UserAccountRecord) -> UserAccountSnapshot:
        return UserAccountSnapshot(
            user_id=record.id,
            organization_id=record.organization_id,
            normalized_username=record.normalized_username,
            display_name=record.display_name,
            password_hash=record.password_hash,
            status=record.status,
            must_change_password=record.must_change_password,
            credential_version=record.credential_version,
            locked_until=record.locked_until,
        )
