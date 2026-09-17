"""`AuditService.append(draft, uow)`：加入业务UnitOfWork，使高风险动作与审计同事务提交。"""

from __future__ import annotations

from knowledge_system.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

from ..domain.audit_event import AuditEventDraft


class AuditService:
    """审计追加的应用层入口；不自行开启或提交事务，事务边界由调用方持有。"""

    async def append(self, draft: AuditEventDraft, uow: SqlAlchemyUnitOfWork) -> None:
        await uow.audit.append(draft)
