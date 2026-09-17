"""独立短事务审计写入器：为登录等“审计与业务结果不在同一事务”的场景提供边界。"""

from __future__ import annotations

from collections.abc import Callable

from knowledge_system.modules.audit.application.audit_service import AuditService
from knowledge_system.modules.audit.domain.audit_event import AuditEventDraft

from .unit_of_work import SqlAlchemyUnitOfWork


class SqlAlchemyAuditWriter:
    """每次追加开启独立UnitOfWork并提交；失败关闭整体回滚。"""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], SqlAlchemyUnitOfWork],
        audit: AuditService,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._audit = audit

    async def append_committed(self, draft: AuditEventDraft) -> None:
        async with self._unit_of_work_factory() as unit_of_work:
            await self._audit.append(draft, unit_of_work)
            await unit_of_work.commit()
